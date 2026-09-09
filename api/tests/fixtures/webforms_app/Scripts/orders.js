function bindOrderGrid() {
    $('#<%= btnSave.ClientID %>').on('click', function () {
        __doPostBack('ctl00$btnSave', '');
    });
    refreshTotals();
}

function refreshTotals() {
    $.ajax({ url: '/Services/OrderService.asmx/GetTotal', type: 'POST' });
}

class OrderGrid {
    reload() { bindOrderGrid(); }
}
