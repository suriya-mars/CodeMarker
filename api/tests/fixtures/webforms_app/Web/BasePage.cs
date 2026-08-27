using System;
using System.Web.UI;

namespace Acme.Web {
    public class BasePage : Page {
        protected void Audit(string action) {
            Session["lastAction"] = action;
        }
    }
}
