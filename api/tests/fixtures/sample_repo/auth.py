from utils import log


class AuthService:
    def login(self, user):
        log("login attempt")
        return self._check(user)

    def _check(self, user):
        return user == "admin"


def authenticate(user):
    service = AuthService()
    return service.login(user)
