**Plan odoo mcp dùng được ở nhiều odoo server**

- **Addon**: phải cài trên từng server, mỗi server là một URL/connector riêng, và khác phiên bản Odoo thì phải chỉnh code (controller, API key, cú pháp thay đổi giữa 16/17/18/19).
- **Gateway tách biệt**: chỉ cài một chỗ, gọi Odoo từ xa qua API. Odoo 19 có API JSON-2 (bearer API key, header chọn database) nên lớp truy cập rất gọn, và phân quyền theo user vẫn giữ nguyên.

Với nhiều server, mình điều chỉnh lại đề xuất trước: **viết phần lõi dùng chung, còn addon hay gateway chỉ là lớp vỏ mỏng**. Không nên khóa cứng vào addon.

## Blueprint

**1. Cấu trúc code**
```
odoo-mcp/
  core/
    tools/          # định nghĩa tool (tên, schema, hàm xử lý), thuần Python
    backend.py      # interface: search_read, read, create, write, call
    mcp_protocol.py # xử lý initialize / tools/list / tools/call
  backends/
    json2.py        # Odoo 19 (HTTP JSON-2)
    xmlrpc.py       # Odoo ≤ 18
    orm.py          # chỉ dùng khi chạy trong addon
  gateway/          # service riêng (FastAPI/FastMCP), đọc registry server
  addon/odoo_mcp/   # tùy chọn: controller mỏng + cấu hình quyền tool
```
Tool chỉ gọi `backend`, không biết đang chạy ở đâu hay Odoo phiên bản nào.

**2. Registry nhiều server** (file cấu hình hoặc DB)
```yaml
servers:
  odoo_a: {url: https://a.example.com, db: prod, version: 19, backend: json2}
  odoo_b: {url: https://b.example.com, db: prod, version: 17, backend: xmlrpc}
```

**3. Cách định tuyến (chọn 1)**
- **Mỗi server một endpoint**: `/mcp/odoo_a`, `/mcp/odoo_b`, mỗi cái là một connector. An toàn nhất, AI không nhầm dữ liệu giữa các server. *Nên bắt đầu bằng cách này.*
- **Một endpoint, tool có tham số `server`**: gọn hơn cho user, nhưng dễ nhầm và khó phân quyền.

**4. Xác thực**
- User dùng API key Odoo của chính họ trên từng server, gateway chỉ chuyển tiếp/lưu mã hóa. Access rights và record rules của Odoo áp dụng nguyên vẹn.
- Sau này có SSO/OAuth thì thêm lớp ánh xạ user, không đổi tool.

**5. Lộ trình**

| Giai đoạn | Việc làm | Kết quả |
|---|---|---|
| 0. Chuẩn bị | Liệt kê server (phiên bản, mạng, số user), chọn 1 server Odoo 19 làm thử nghiệm | Danh sách + server pilot |
| 1. Lõi | Viết `core` + `json2` backend, 2-3 tool chỉ-đọc (tìm khách hàng, xem đơn hàng), chạy local để học giao thức | Chạy được trên Claude Desktop/Code |
| 2. Remote | Đóng gói Docker, HTTPS, endpoint `/mcp/odoo_a`, log + rate limit | Dùng được qua URL |
| 3. Nhiều server | Thêm registry, thêm server Odoo 19 khác, rồi viết `xmlrpc` backend cho bản cũ | Nhiều connector |
| 4. Tool ghi | Thêm tool tạo/sửa có bước xác nhận, chỉ mở cho nhóm nhất định | Nghiệp vụ thực tế |
| 5. Vận hành | Giám sát, xoay key, test khi nâng cấp Odoo | Ổn định lâu dài |

Addon chỉ cần làm ở giai đoạn 4-5 nếu muốn quản lý quyền tool ngay trong giao diện Odoo.

**6. Lưu ý riêng cho Odoo 19**
- Ưu tiên JSON-2. XML-RPC/JSON-RPC cũ đã được thông báo loại bỏ ở bản sau, nên đừng xây mới trên chúng cho server 19 (bạn nên kiểm tra tài liệu Odoo để xác nhận mốc chính xác).
- Nếu một server có nhiều database, cấu hình DB trong registry.

Bạn có bao nhiêu server và bản nào (ngoài Odoo 19)? Nếu muốn, mình sẽ viết luôn bộ khung code cho `core` + `json2` backend + 2 tool đầu tiên.