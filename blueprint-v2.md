# Odoo MCP Gateway — Blueprint

## Mục tiêu

Một MCP gateway tách biệt (không phải addon Odoo), cho phép:
- Nhiều người dùng tự kết nối AI (Claude) tới nhiều Odoo server, dùng **chính quyền Odoo của họ**.
- AI thao tác được với **bất kỳ model nào trong allowlist**, không giới hạn ở vài tool viết sẵn.
- Người dùng chỉ cần chat, không cần biết tool nào đang chạy phía sau.

```
User (Claude) --OAuth--> MCP Gateway (HTTP) --API key của user--> Odoo (JSON-2 / XML-RPC)
```

---

## Kiến trúc

| Lớp | Vai trò |
|---|---|
| `core/tools` | Tool tổng quát theo model (không phải theo nghiệp vụ cố định) |
| `core/backend` | Contract: `search_read`, `read`, `create`, `write`, `fields_get` |
| `backends/` | `Json2Backend` (Odoo 19+), `XmlRpcBackend` (≤18), `FakeBackend` (test) |
| `gateway/` | `stdio` (local, 1 user/1 server) + `http` (remote, nhiều user/nhiều server) |
| `auth/` | OAuth (ưu tiên Microsoft Entra ID) + credential vault theo user |
| `config/registry` | YAML registry: server, model allowlist, field allowlist |

---

## Thay đổi so với bản đầu: Tool tổng quát thay vì cố định

Bỏ dần cách "1 tool = 1 nghiệp vụ" (`search_partners`, `get_partner`...). Thay bằng tool theo model, AI tự tra schema rồi gọi:

| Tool | Chức năng |
|---|---|
| `list_models` | Liệt kê model được phép dùng (đọc từ allowlist của server) |
| `describe_model(model)` | Trả field + kiểu dữ liệu (`fields_get`) để AI biết cấu trúc trước khi truy vấn |
| `search_read(model, domain, fields, limit)` | Đọc model bất kỳ trong allowlist |
| `create(model, values)` | Tạo bản ghi — có `confirm` + field allowlist |
| `write(model, ids, values)` | Sửa bản ghi — có `confirm` + field allowlist |

`ping` giữ nguyên làm health check.

**An toàn vẫn giữ 2 lớp allowlist** (không mở `execute_kw` vô điều kiện):
- **Model allowlist**: chỉ mở model được khai báo (`res.partner`, `sale.order`, `crm.lead`...), không đụng tới model hệ thống (`ir.config_parameter`, `res.users`, quyền...).
- **Field allowlist theo model**: đặc biệt quan trọng lúc ghi, tránh AI sửa field nhạy cảm.

Cấu hình mẫu trong `servers.yaml`:
```yaml
odoo_a:
  url: https://a.example.com
  db: prod
  version: 19
  backend: json2
  allowed_models:
    res.partner:
      read: all
      write: [name, email, phone]
    sale.order:
      read: all
      write: []
  write_tools: [create, write]
```

---

## Thay đổi so với bản đầu: Auth theo từng người dùng

Không dùng chung 1 `ODOO_API_KEY` nữa. Mỗi user xác thực bằng danh tính riêng, gateway tra ra API key Odoo tương ứng của người đó rồi mới gọi Odoo — nhờ vậy ACL/record rules của Odoo áp đúng theo từng người, và audit log biết chính xác ai đã làm gì.

**IdP ưu tiên: Microsoft Entra ID** (công ty đang dùng sẵn). Chi tiết kỹ thuật cụ thể (đăng ký app, redirect URI, scope...) sẽ tìm hiểu ở giai đoạn triển khai OAuth — chưa chốt ở bước blueprint này.

**Luồng dự kiến:**
```
1. User mở Claude, thêm connector trỏ tới odoo-mcp-http
2. Claude redirect user sang đăng nhập Microsoft (OAuth 2.1)
3. Gateway nhận danh tính user (email/ID từ Microsoft)
4. Lần đầu: user mở trang "Connect Odoo account", dán API key Odoo
   của họ cho từng server muốn dùng
5. Gateway mã hóa, lưu key gắn với danh tính user (credential vault)
6. Từ đó mỗi tool call: gateway lấy identity từ token OAuth
   → tra vault → dùng đúng API key của user đó gọi Odoo
```

**Thành phần cần thêm:**
- Xác thực OAuth với Microsoft Entra ID làm authorization/identity provider.
- **Credential vault**: bảng `user_identity → {server_id: encrypted_api_key}`, mã hóa khi lưu.
- Trang "Connect Odoo account" tối giản: đăng nhập Microsoft xong, dán API key Odoo một lần cho mỗi server.
- Audit log theo user thật (không chỉ theo server như hiện tại).

**Việc "AI dùng tool mà user không cần biết"**: không cần code thêm — Claude tự chọn tool dựa trên mô tả, đây là hành vi mặc định.

---

## Hai cách chạy

1. **stdio** (`odoo-mcp-stdio`) — chạy local, dùng 1 server + 1 API key qua biến môi trường. Phù hợp cho dev/test cá nhân, không phù hợp cho nhiều user.
2. **HTTP** (`odoo-mcp-http`) — mỗi server một endpoint `/mcp/{id}`, có OAuth + credential vault, log + rate limit theo user. Đây là hướng chính cho việc nhiều người dùng.

---

## Roadmap cập nhật

| Phase | Nội dung | Trạng thái |
|---|---|---|
| 3 | MCP stdio + `ping` | ✅ Đã xong |
| 4 | Read tools E2E qua Json2 | ✅ Đã xong (dạng tool cố định, sẽ generalize ở phase 11) |
| 5 | Server registry YAML | ✅ Đã xong |
| 6 | HTTP gateway, log, rate limit | ✅ Đã xong (theo server, chưa theo user) |
| 7 | Docker + reverse proxy HTTPS | ✅ Đã xong |
| 8 | Multi-server + XmlRpcBackend | ✅ Đã xong |
| 9 | Write tool + confirm + allowlist | ✅ Đã xong (allowlist theo tool, sẽ mở rộng theo field/model) |
| 10 | Ops: rotate key, smoke after upgrade | ✅ Đã xong |
| **11** | **Tool tổng quát**: `list_models`, `describe_model`, `search_read`, `create`, `write` theo model + allowlist model/field trong registry | ⬜ Cần làm |
| **12** | **OAuth với Microsoft Entra ID** + credential vault theo user + trang "Connect Odoo account" | ⬜ Cần làm |
| **13** | Audit log theo user thật | ⬜ Cần làm |
| 14 | Monitoring/alerting tự động (ngoài smoke script chạy tay) | ⬜ Cần làm |
| 15 (tùy chọn) | Addon Odoo trong-app (quản lý quyền tool ngay trong giao diện Odoo) | ⬜ Để sau |

---

## Việc cần bổ sung, ưu tiên

| Việc | Mức độ ưu tiên | Ghi chú |
|---|---|---|
| Tool tổng quát theo model (Phase 11) | Cao | Thay thế dần tool cố định hiện tại |
| Model + field allowlist trong registry | Cao | Bù rủi ro khi bỏ giới hạn tool cứng |
| OAuth Microsoft Entra ID + credential vault (Phase 12) | Cao | Cần trước khi mở cho nhiều người dùng thật |
| Trang "Connect Odoo account" | Trung bình | Chỉ cần 1 lần/user/server |
| Audit log theo user thật | Trung bình | Làm cùng lúc với Phase 12 |
| Monitoring/alerting tự động | Trung bình | Hiện chỉ có smoke script chạy tay |