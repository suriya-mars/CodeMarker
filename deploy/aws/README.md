# Deploying RepoDigger to AWS

One customer, one stack: a private EC2 instance running `docker-compose.prod.yml`,
behind an HTTPS load balancer that requires a Cognito login. Terraform in
`terraform/` creates everything; nothing secret ever goes into Terraform, git, the
image or the instance's user data.

```
users ──HTTPS──► ALB (Cognito login) ──► EC2, private subnet ──► web ─► api ─► neo4j
GitHub webhook ─► ALB (no login; the api checks GitHub's signature)
EC2 ──HTTPS via NAT──► LLM provider, git hosts, GitHub, AWS APIs
```

| What | Where |
|---|---|
| Graph, clones, images | Encrypted EBS data volume, kept if the instance is replaced, snapshotted daily |
| Secrets | AWS Secrets Manager, rendered into a root-only `.env` at each start |
| Full backups | Weekly Neo4j dump to a versioned, private S3 bucket |
| Logs | CloudWatch (counts and events only, never prompts or source) |
| Shell access | Session Manager, no SSH, no open ports |

**Rough cost** (us-east-1, on-demand, check current prices): m6i.xlarge ~$140/month,
load balancer ~$20, NAT gateway ~$35 plus data, 100 GB gp3 ~$8, S3 and logs a few
dollars. The LLM bill is separate and depends on use.

---

## Before you start

- An AWS account, and Terraform ≥ 1.6 with credentials for it.
- A domain name. With a Route 53 zone, set `route53_zone_id` and the certificate and
  DNS record are created for you; otherwise create an ACM certificate yourself and set
  `certificate_arn`.
- This repository pushed somewhere the instance can clone over https. For a private
  repository, put a read-only token in the secret as `GIT_DEPLOY_TOKEN` (step 3).
- **Decide who may log in.** There is no admin role yet: anyone who can log in can add
  a repository, and indexing spends LLM tokens. Create Cognito users only for people
  who should have that.

---

## 1. Create the infrastructure

```bash
cd deploy/aws/terraform
cp terraform.tfvars.example terraform.tfvars     # fill in your values
terraform init
terraform plan
terraform apply
```

For anything beyond a trial, keep Terraform state in S3: uncomment the `backend`
block in `versions.tf` first. State lists every resource, though never a secret value.

The load balancer and the data volume are protected from `terraform destroy`
(deletion protection and `prevent_destroy`). That is deliberate.

## 2. Write the secret

Create `repodigger-secret.json` locally (it is git-ignored). One line per value, no
single quotes, upper-case keys:

```json
{
  "NEO4J_PASSWORD": "a long random password",
  "LLM_PROVIDER": "openai",
  "OPENAI_API_KEY": "sk-...",
  "OPENAI_MODEL": "gpt-4o-mini",
  "DAILY_TOKEN_QUOTA": "2000000",
  "GIT_DEPLOY_TOKEN": "only if this repository is private"
}
```

Any setting from `.env.example` can go here (`GITHUB_WEBHOOK_SECRET`, `CLONE_TOKEN`,
`SESSION_RETENTION_DAYS`, ...). `AUTH_MODE`, `ALB_ARN` and `PUBLIC_URL` are set by the
machine and ignored if present. Upload it, then delete the local file:

```bash
$(terraform output -raw set_secret_command)
shred -u repodigger-secret.json
```

## 3. Start the app

The instance installs Docker and the app on first boot, but cannot start until the
secret has values. Start it once:

```bash
$(terraform output -raw shell_command)            # opens a shell on the instance
sudo systemctl start repodigger                  # first build takes ~10 minutes
sudo journalctl -u repodigger -f                 # follow progress
```

From then on it starts by itself after every reboot.

## 4. Log in and check

1. Each address in `initial_user_emails` receives a temporary password. Add more
   people with the command in `terraform output add_user_command`.
2. Open `terraform output -raw url`, log in, and set a new password.
3. In the **Repos** tab, add a small public repository and watch it index.
4. Ask a question. Check `/api/me/usage` shows your email and today's cost.

## 5. Do a restore drill now, not when you need it

A backup you have never restored is a hope, not a backup.

```bash
sudo systemctl start repodigger-backup           # stops the database ~1 minute
aws s3 ls s3://$(terraform output -raw backup_bucket)/neo4j/
```

Then restore that dump into a scratch database on the instance and compare counts:

```bash
cd /opt/repodigger
aws s3 cp s3://<bucket>/neo4j/<file>.dump /var/tmp/restore/neo4j.dump
sudo chown -R 7474:7474 /var/tmp/restore
docker volume create restore-drill
docker run --rm -v restore-drill:/data -v /var/tmp/restore:/backups:ro \
  neo4j:5.26.27-community neo4j-admin database load neo4j --from-path=/backups
docker run -d --rm --name drill -v restore-drill:/data -e NEO4J_AUTH=neo4j/drillpassword \
  -e NEO4J_dbms_usage__report_enabled=false neo4j:5.26.27-community
sleep 30
docker exec drill cypher-shell -u neo4j -p drillpassword \
  "MATCH (s:Symbol) RETURN s.repo, count(*)"      # compare with the live app's Repos tab
docker rm -f drill && docker volume rm restore-drill && rm -rf /var/tmp/restore
```

---

## Operating it

**Deploy a new version**
```bash
sudo /opt/repodigger/deploy/update.sh            # or: update.sh <branch or tag>
```

**Remove a person** (a leaver, or a deletion request):
```bash
cd /opt/repodigger
docker compose -f docker-compose.prod.yml run --rm api python -m quota forget alice@example.com --yes
aws cognito-idp admin-delete-user --user-pool-id <pool id> --username alice@example.com
```
Backups and snapshots still hold their data until they expire (S3 dumps after
`backup_retention_days`, snapshots after `snapshot_retention_count` days).

**Rotate a secret:** update it in Secrets Manager, then `sudo systemctl restart repodigger`.

**Logs:** CloudWatch log group `/repodigger/<name>`, one stream per container.

**Restore for real** (the data volume is lost or corrupted):
1. Prefer the newest daily EBS snapshot: create a volume from it in the instance's
   availability zone, stop the app, swap the volume, start the app.
2. Or load the newest S3 dump into the live database:
   ```bash
   sudo systemctl stop repodigger
   docker compose -f docker-compose.prod.yml run --rm --no-deps -v /var/tmp/restore:/backups:ro \
     neo4j neo4j-admin database load neo4j --from-path=/backups --overwrite-destination=true
   sudo systemctl start repodigger
   ```
   Repositories added by URL are still cloned on the data volume; re-adding a URL
   after a restore picks its existing clone back up.

**Replace the instance** (new AMI, new size): change `instance_type` or taint the
instance and `terraform apply`. The data volume is detached and re-attached; nothing
on it is lost.

---

## Security notes

- The instance has no public address and no SSH. Only the load balancer reaches it,
  on one port.
- Containers cannot read the instance's AWS credentials (IMDSv2 with a hop limit of 1).
- The api container runs read-only, without Linux capabilities, as a non-root user.
- Login tokens are verified against this load balancer's ARN; a token from any other
  load balancer is refused.
- Outbound traffic is limited to HTTPS, but not by host name. If you need an allowlist
  (only your LLM provider and git host), add AWS Network Firewall in front of the NAT.
- Narrow `allowed_cidrs` to your offices or VPN if you can. If you also use PR
  comments, add GitHub's hook ranges (`curl https://api.github.com/meta | jq .hooks`)
  to `github_webhook_cidrs`.
- Consider AWS WAF on the load balancer, and turn on ALB access logs if you need an
  audit trail of requests.

## Limits of this setup

- **One api container.** The index queue and rate limiter live in its memory, so it
  cannot be scaled out yet. A team-sized stack does not need to.
- **One availability zone** for the instance. An AWS zone outage takes it down until
  you restore elsewhere from a snapshot or dump.
- **Weekly dumps stop the database for about a minute** (Neo4j Community cannot dump a
  running database). Daily snapshots run without downtime.
