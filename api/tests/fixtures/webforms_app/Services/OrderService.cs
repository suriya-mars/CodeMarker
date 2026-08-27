using System.Data;
using System.Data.SqlClient;

namespace Acme.Services {
    public class OrderService : IOrderService {
        public int Save(string order) {
            using (var cmd = new SqlCommand("usp_SaveOrder", _conn)) {
                cmd.CommandType = CommandType.StoredProcedure;
                return cmd.ExecuteNonQuery();
            }
        }
        public int Count() {
            var cmd = new SqlCommand();
            cmd.CommandText = "usp_CountOrders";
            return 0;
        }
    }
}
