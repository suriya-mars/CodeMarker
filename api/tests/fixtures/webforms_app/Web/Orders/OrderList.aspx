<%@ Page Language="C#" AutoEventWireup="true"
    CodeBehind="OrderList.aspx.cs" Inherits="Acme.Web.Orders.OrderList" %>
<html><body>
  <form runat="server">
    <asp:Button ID="btnSave" runat="server" Text="Save" OnClick="btnSave_Click" />
    <asp:GridView ID="grid" runat="server" OnRowDataBound="grid_RowDataBound" />
  </form>
</body></html>
