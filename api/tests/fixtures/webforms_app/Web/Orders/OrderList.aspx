<%@ Page Language="C#" AutoEventWireup="true"
    CodeBehind="OrderList.aspx.cs" Inherits="Acme.Web.Orders.OrderList" %>
<head>
  <link rel="stylesheet" href="~/Content/site.css" />
  <link rel="stylesheet" href="~/Content/bootstrap-3.3.7.css" />
  <script src="~/Scripts/lib/jquery-1.9.1.min.js"></script>
  <script src="~/Scripts/orders.js"></script>
  <script type="text/javascript">
    $(function () { __doPostBack('ctl00$grid', ''); });
  </script>
</head>
<body>
  <form runat="server">
    <asp:Button ID="btnSave" runat="server" Text="Save" OnClick="btnSave_Click" />
    <asp:GridView ID="grid" runat="server" OnRowDataBound="grid_RowDataBound" />
    <asp:UpdatePanel ID="up1" runat="server"><ContentTemplate/></asp:UpdatePanel>
  </form>
</body></html>
