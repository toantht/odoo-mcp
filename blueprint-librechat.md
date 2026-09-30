# LibreChat — self-host UI gọi Claude API qua odoo-mcp

## Mục tiêu

Một web UI tự host, người dùng chat với Claude API, tool Odoo lấy từ gateway
`odoo-mcp` đã có ([blueprint-v3.md](blueprint-v3.md)). LibreChat đứng ở vị trí
Claude.ai trong blueprint v3: một MCP client OAuth, không phải phần mở rộng
của gateway.

Không đổi gateway để làm việc này. Phase 12 của blueprint v3 (Streamable
HTTP tại `/mcp/{id}`, OAuth 2.1 + PKCE, dynamic client registration, vault
theo bearer token) đã đủ cho bất kỳ MCP client OAuth nào, không riêng Claude.

LibreChat không nằm trong `/src`. `/src` là package Python `odoo_mcp`
([pyproject.toml](pyproject.toml)); LibreChat là app Node riêng (API,
MongoDB, Meilisearch) với Docker image chính thức
(`ghcr.io/danny-avila/librechat`). Blueprint này chỉ trỏ image đó, không
vendor source. Nếu sau này cần file chạy được trong repo (compose + env
example), đặt ở `deploy/librechat/`, tách khỏi `src/odoo_mcp` và
`addon/odoo_mcp`.

```
User browser --> LibreChat (image chính thức)
                     |  Claude API (key riêng của LibreChat)
                     v
                 Anthropic

                 LibreChat --MCP OAuth, token riêng từng user--> odoo-mcp /mcp/{id}
                                                                      |
                                                                      v
                                                                 Odoo addon (consent, API key)
```

Một user LibreChat = một token MCP = một API key Odoo của đúng user đó.
Quyền đọc/viết vẫn là ACL Odoo của user vừa bấm cho phép, không phải quyền
chung của LibreChat.

---

## Điều kiện gateway phải có sẵn

- OAuth đang bật: `ODOO_MCP_ALLOW_SHARED_KEY` không được set (xem
  [.env.example](.env.example)). Chế độ shared-key không có bước consent,
  không hợp với nhiều user LibreChat.
- `ODOO_MCP_PUBLIC_URL` là một HTTPS public thật — đây là issuer trong OAuth
  metadata mà LibreChat sẽ đọc. LibreChat phải gọi đúng URL này, không gọi
  hostname nội bộ Docker của gateway.
- `addon/odoo_mcp` đã cài trên từng Odoo server muốn mở, channel secret
  (`ODOO_MCP_CHANNEL_SECRET_<SERVER_ID>`) khớp giữa gateway và Settings > MCP
  Gateway trên Odoo.
- Gateway chạy một process duy nhất. Giao dịch OAuth đang chờ nằm trong RAM
  (`_pending_txns`, [auth/provider.py](src/odoo_mcp/auth/provider.py)), vault
  là SQLite ([auth/vault.py](src/odoo_mcp/auth/vault.py)). Nhiều replica sẽ
  làm callback rơi nhầm instance hoặc ghi vault đè nhau — ngoài phạm vi
  blueprint này.

---

## Cấu hình LibreChat

Mỗi Odoo server trong `config/servers.yaml` là một mục `mcpServers` trong
`librechat.yaml`:

```yaml
mcpServers:
  odoo_a:
    type: streamable-http
    url: https://mcp.example.com/mcp/odoo_a
    requiresOAuth: true
    startup: false
```

- Không khai `oauth.client_id` — gateway đã bật dynamic client registration
  (`client_registration_options` trong
  [gateway/http.py](src/odoo_mcp/gateway/http.py)); LibreChat tự đọc
  protected-resource metadata rồi đăng ký client.
- `startup: false` để process LibreChat không thử kết nối bằng một user hệ
  thống lúc khởi động — kết nối chỉ nên xảy ra khi một user thật bấm
  Authenticate.
- Callback LibreChat cho server này: `${DOMAIN_SERVER}/api/mcp/odoo_a/oauth/callback`.

Endpoint Claude (Anthropic) cấu hình tách biệt, key riêng của LibreChat —
không liên quan tới key Odoo.

---

## Phía gateway: mở allowlist redirect cho LibreChat

`ODOO_MCP_OAUTH_REDIRECT_HOSTS` ([.env.example](.env.example)) mặc định chỉ
`claude.ai` / `localhost` / `127.0.0.1`
(`_DEFAULT_OAUTH_REDIRECT_HOSTS`, [gateway/http.py](src/odoo_mcp/gateway/http.py)).
Thêm host LibreChat:

```dotenv
ODOO_MCP_OAUTH_REDIRECT_HOSTS=chat.example.com
```

Addon Odoo **không** cần biết callback của LibreChat. Trình duyệt luôn quay
về gateway trước (`{ODOO_MCP_PUBLIC_URL}/oauth/odoo-callback`), gateway mới
redirect tiếp về `redirect_uri` mà client (LibreChat) đăng ký. Allowlist
trong Settings > MCP Gateway trên Odoo
([addon/odoo_mcp/models/res_config_settings.py](addon/odoo_mcp/models/res_config_settings.py))
chỉ chứa URL callback đó của gateway, không đổi khi thêm LibreChat.

---

## Auth và chi phí

- Tắt đăng ký mở trên LibreChat (`ALLOW_REGISTRATION=false` hoặc tương đương)
  — mỗi account LibreChat dùng chung key Anthropic của instance, người lạ tạo
  tài khoản là tốn token dù chưa từng consent Odoo.
- Consent Odoo là lớp độc lập, xảy ra sau khi user đã đăng nhập LibreChat và
  bấm Authenticate MCP — hai lớp xác thực khác nhau, không thay thế nhau.

---

## Ghi dữ liệu vẫn cần cẩn trọng

`confirm=true` trên tool `create`/`write` ([core/mcp_server.py](src/odoo_mcp/core/mcp_server.py))
là cờ do model tự gửi trong tool call, không phải nút duyệt hiển thị trên UI
LibreChat. Với deployment LibreChat, để agent/model spec mặc định không bật
`create`/`write`, hoặc để server đó trong `servers.yaml` với `write_tools:
[]` nếu chỉ cần đọc.

---

## Checklist chạy thử

1. `curl {ODOO_MCP_PUBLIC_URL}/healthz` — `oauth_enabled: true`, server cần
   dùng có trong danh sách.
2. Đăng nhập LibreChat bằng account A, mở MCP Settings, Authenticate server
   `odoo_a` — trình duyệt phải qua `/web/login` (nếu chưa có phiên) rồi tới
   trang đồng ý của addon, không phải lỗi redirect_uri.
3. Bấm Allow — Account Security của user A trên Odoo có API key mới tên
   `Claude MCP`; LibreChat báo server đã Connected.
4. Từ chat của account A, gọi một tool đọc (`search_read`) — dữ liệu trả về
   đúng quyền của user A (thử với user có quyền đọc bị giới hạn để xác nhận
   ACL áp đúng).
5. Đăng nhập LibreChat bằng account B, Authenticate lại — xác nhận B nhận
   token/API key riêng, không tái dùng token của A, và dữ liệu B thấy khác A
   nếu ACL Odoo khác nhau.
6. Bấm Deny ở một lần Authenticate mới — không có API key `Claude MCP` mới
   nào được tạo, LibreChat báo lỗi authorization thay vì Connected.

---

## Ngoài phạm vi

- Audit log theo `uid` Odoo cho từng tool call (phase 13 của blueprint v3) —
  LibreChat có log chat riêng của nó, nhưng gateway chưa ghi user Odoo nào
  gọi tool nào.
- Monitoring/alerting tự động (phase 14).
- Chạy gateway nhiều replica — vault SQLite và OAuth pending-transaction
  trong RAM hiện chỉ đúng cho một process.
- Sửa `_allowed_redirect_hosts`/logic OAuth trong code — chỉ cần đổi biến
  môi trường `ODOO_MCP_OAUTH_REDIRECT_HOSTS`, không cần patch
  [gateway/http.py](src/odoo_mcp/gateway/http.py).
