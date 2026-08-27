using System;
using Acme.Services;

namespace Acme.Web.Orders {
    public partial class OrderList : BasePage {
        private readonly IOrderService _svc = new OrderService();

        protected void Page_Load(object sender, EventArgs e) {
            if (!IsPostBack) { BindGrid(); }
        }

        protected void btnSave_Click(object sender, EventArgs e) {
            _svc.Save(Request.Form["order"]);
            Audit("save");
            BindGrid();
        }

        protected void grid_RowDataBound(object sender, EventArgs e) { }

        private void BindGrid() {
            ViewState["bound"] = true;
        }
    }
}
