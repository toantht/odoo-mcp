# Odoo MCP Gateway — Blueprint v3

## Mục tiêu

Một MCP gateway tách biệt, cho phép nhiều người dùng tự kết nối AI (Claude) tới nhiều Odoo server, dùng **đúng quyền Odoo của họ**. Người dùng không dán API key và không cần biết tool nào đang chạy.

Đăng nhập xảy ra trên chính Odoo server đó. Đã có phiên thì chỉ bấm cho phép Claude kết nối. Gateway tự nhận API key do Odoo tạo ra sau bước đồng ý.

```
Claude --OAuth--> MCP Gateway /mcp/{id}
                      |  chuyển trình duyệt
                      v
                 Odoo /web/login          (bỏ qua nếu đã có phiên)
                      |
                      v
                 Trang "Cho phép Claude kết nối với quyền của bạn?"
                      |  auth code, kênh server-to-server
                      v
                 Gateway lưu API key đã mã hóa
                      |
                      v
                 Odoo JSON-2 (19+) hoặc XML-RPC (≤18), đúng user vừa đồng ý
```

Claude không nói chuyện trực tiếp với Odoo. Sau khi đóng trình duyệt, gateway vẫn gọi Odoo bằng API key gắn user đó. ACL và record rules của Odoo áp nguyên.

---

## Kiến trúc

| Lớp | Vai trò |
|---|---|
| `core/tools` | Tool tổng quát theo model (không phải theo nghiệp vụ cố định) |
| `core/backend` | Contract: `search_read`, `read`, `create`, `write`, `fields_get` |
| `backends/` | `Json2Backend` (Odoo 19+), `XmlRpcBackend` (≤18), `FakeBackend` (test) |
| `gateway/` | `stdio` (local, 1 user/1 server) + `http` (remote, nhiều user/nhiều server) |
| `auth/` | OAuth 2.1 phía Claude (PKCE) + đổi code lấy API key + credential vault |
| `addon/odoo_mcp/` | Addon mỏng trên từng Odoo: đăng nhập sẵn có, trang đồng ý, tạo API key |
| `config/registry` | YAML registry: server, model allowlist, field allowlist, secret kênh nội bộ |

Mỗi Odoo server là một connector `/mcp/{id}`. AI không lẫn dữ liệu giữa các server. Kết nối server thứ hai là một lần đăng nhập và một lần bấm cho phép trên server đó.

---

## Tool tổng quát

Giữ hướng của blueprint v2. Bỏ dần "1 tool = 1 nghiệp vụ" (`search_partners`, `get_partner`...). AI tự tra schema rồi gọi:

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
servers:
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

Secret dùng để gateway đổi auth code với addon **không** nằm trong file này. Đặt ở biến môi trường hoặc secret store, mỗi server một giá trị.

---

## Auth: đăng nhập Odoo, rồi bấm cho phép

Thay hướng blueprint v2 (Microsoft Entra ID làm IdP của MCP, user dán API key). Entra không còn là lớp đăng nhập của gateway. Nếu Odoo của công ty đăng nhập bằng Microsoft, màn hình đó vẫn là `/web/login` của Odoo — gateway không tích hợp Entra.

Danh tính trên gateway là user Odoo vừa đồng ý, không phải email Microsoft ánh xạ sang key.

### Việc user thấy

1. Trong Claude, thêm connector trỏ tới `https://<gateway>/mcp/odoo_a`.
2. Trình duyệt mở Odoo server `odoo_a`.
3. Chưa đăng nhập: trang đăng nhập Odoo, rồi quay lại bước đồng ý.
4. Đã đăng nhập: một trang, tên user và tên server, nút cho phép hoặc từ chối.
5. Bấm cho phép là xong. Không copy API key.

Thu hồi: user xóa API key tên kiểu `Claude MCP` trong Account Security của Odoo. Lần gọi sau thất bại, Claude phải xin phép lại.

### Việc hệ thống làm

Gateway là OAuth server mà Claude nhìn thấy (OAuth 2.1, PKCE, redirect URI cố định). `/authorize` của gateway chỉ chuyển trình duyệt sang addon.

Addon trên Odoo, route kiểu `/odoo_mcp/authorize`:

1. Chưa có `session.uid` thì redirect `/web/login?redirect=...`.
2. Đã có phiên thì render trang đồng ý.
3. Khi đồng ý, tạo API key bằng `res.users.apikeys` cho đúng user đó, lưu auth code một lần, hạn ngắn.
4. Trả code về redirect của gateway. **API key không đi qua trình duyệt.**

Gateway đổi code bằng kênh server-to-server, kèm secret đã cấu hình với addon. Addon trả API key, login, uid. Gateway mã hóa và lưu vault:

```
mcp access token → {server_id, odoo_uid, odoo_login, encrypted_api_key}
```

Mỗi tool call: lấy bearer token của Claude → tra vault → `Json2Backend` hoặc `XmlRpcBackend` gọi Odoo bằng key đó. Backend hiện tại không đổi hợp đồng; chỉ nguồn key đổi từ một `ODOO_API_KEY` chung sang key theo phiên MCP.

### Vì sao vẫn cần API key

Phiên trình duyệt chết khi đóng tab, và gateway khác domain nên không dùng được cookie Odoo. Key là credential lâu dài để các lần chat sau vẫn chạy. User không thấy key; vault chỉ là chỗ cất key do bước đồng ý tạo ra.

### Addon mỏng, bắt buộc cho HTTP nhiều user

Blueprint v2 để addon sang phase tùy chọn. Với hướng này, addon là bắt buộc trên từng server muốn mở connector, vì chỉ Odoo biết trình duyệt đang là user nào và chỉ Odoo tạo được API key trong tên user đó. Addon không chứa tool MCP và không thay backend: login, trang đồng ý, tạo key, đổi code.

Gateway không nhận mật khẩu Odoo và không đọc session cookie của Odoo.

### Vận hành: gateway sau reverse proxy / tunnel

`FastMCP` tự bật chống DNS-rebinding cho tầng transport MCP, mặc định chỉ
cho phép Host/Origin `127.0.0.1`/`localhost` — độc lập với OAuth bearer
token, kiểm trước cả khi vào tới tool call. Gateway đứng sau domain khác
(reverse proxy Phase 7, hoặc tunnel `ngrok`/`cloudflared` lúc test) phải
thêm đúng host của `ODOO_MCP_PUBLIC_URL` vào allowlist đó, nếu không mọi
`/mcp/{id}` đều `421 Invalid Host header` dù OAuth đã đúng. Phát hiện khi
test Phase 12 thật qua tunnel Cloudflare; đã fix trong
[`gateway/http.py`](src/odoo_mcp/gateway/http.py)'s `_transport_security`.

---

## Hai cách chạy

1. **stdio** (`odoo-mcp-stdio`) — chạy local, một server, một API key qua biến môi trường. Dùng cho dev/test. Không có trang đồng ý.
2. **HTTP** (`odoo-mcp-http`) — mỗi server một endpoint `/mcp/{id}`, OAuth với Claude, consent trên Odoo, vault theo token, log và rate limit theo user Odoo. Đây là hướng cho nhiều người dùng.

Client OAuth khác Claude.ai (ví dụ self-host web UI) nối vào cùng endpoint HTTP này không cần đổi gateway — xem [blueprint-librechat.md](blueprint-librechat.md).

---

## Roadmap

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
| **11** | **Tool tổng quát**: `list_models`, `describe_model`, `search_read`, `create`, `write` theo model + allowlist model/field trong registry | ✅ Đã xong |
| **12** | **Consent trên Odoo**: addon mỏng (login sẵn có, nút cho phép, tạo API key, đổi code) + OAuth 2.1/PKCE trên gateway + vault điền tự động | ✅ Đã xong — test thật trên pilot Odoo 19 (không chỉ pytest mock) |
| **13** | Audit log theo user Odoo (`uid` / login từ bước đổi code) | ⬜ Cần làm |
| 14 | Monitoring/alerting tự động (ngoài smoke script chạy tay) | ⬜ Cần làm |
| 15 (tùy chọn) | Trong addon: quản lý quyền tool ngay trên giao diện Odoo, thay vì chỉ YAML | ⬜ Để sau |

Phase 11 độc lập, không cần addon.

---

## Việc cần bổ sung, ưu tiên

| Việc | Mức độ ưu tiên | Ghi chú |
|---|---|---|
| Tool tổng quát theo model (Phase 11) | Cao | Thay thế dần tool cố định hiện tại |
| Model + field allowlist trong registry | Cao | Bù rủi ro khi bỏ giới hạn tool cứng |
| Audit log theo user Odoo | Trung bình | Làm cùng Phase 13, sau khi Phase 12 đã có `uid`/login theo request |
| Monitoring/alerting tự động | Trung bình | Hiện chỉ có smoke script chạy tay |

---

## Thay đổi so với blueprint v2

| v2 | v3 |
|---|---|
| IdP của MCP là Microsoft Entra ID | IdP thực tế là phiên đăng nhập Odoo; gateway chỉ là OAuth server với Claude |
| Lần đầu user dán API key từng server | Odoo tự tạo API key khi user bấm cho phép |
| Vault: email/ID Microsoft → key từng server | Vault: MCP access token → key của user Odoo trên đúng server của connector |
| Addon tùy chọn, phase 15 | Addon mỏng bắt buộc ở phase 12; phase 15 chỉ còn phần quản lý quyền tool trong UI Odoo |
