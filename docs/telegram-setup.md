# Telegram — cấu hình thông báo & điều khiển (runbook)

Phần hạ tầng đã có sẵn trong `app/notify.py`: gửi cảnh báo + poller nhận lệnh (chỉ chat
được phép). Làm 4 bước dưới là chạy được NGAY các lệnh `/status /pause /resume /freeze
/reset` và nút "Thử". Các tính năng mới (tổng quan equity/P&L, /pending, /positions, /kss,
push trade/risk) sẽ được xây theo `docs/plan/telegram-notify-plan.md` sau khi bạn duyệt.

## Bước 1 — Tạo bot, lấy TOKEN
1. Mở Telegram, chat với **@BotFather**.
2. Gửi `/newbot` → đặt tên + username (kết thúc bằng `bot`, vd `findmy_fm_bot`).
3. BotFather trả về **token** dạng `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxx`. Giữ bí mật.

## Bước 2 — Lấy CHAT_ID (chat sẽ nhận thông báo & gửi lệnh)
Cách A (nhanh): chat với **@userinfobot**, nó trả về `Id` của bạn → đó là `chat_id`.

Cách B (chính xác cho group/channel):
1. Gửi 1 tin bất kỳ cho bot vừa tạo (vd `hello`).
2. Mở trình duyệt:
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Tìm `"chat":{"id": <số>}` — số đó là `chat_id` (group sẽ là số âm).

> Chỉ **đúng chat_id này** mới nhận alert và được phép gửi lệnh — mọi chat khác bị bỏ qua
> (auth boundary trong [notify.py](app/notify.py)).

## Bước 3 — Khai báo vào `.env`
Thêm/sửa trong file `.env` (KHÔNG commit file này):
```
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=123456789:AAE...        # token từ BotFather
TELEGRAM_CHAT_ID=123456789                  # chat_id ở bước 2
TELEGRAM_POLL_INTERVAL=5                     # giây giữa các lần nhận lệnh (mặc định 5)
```

## Bước 4 — Khởi động lại + kiểm tra
1. Restart app (uvicorn). Boot log sẽ in `notify poller started`.
2. **Trên dashboard**: thanh trạng thái → mục **TELEGRAM** → bấm **Thử** → bạn nhận
   "FINDMY-FM test alert". (Hoặc bật/tắt TELEGRAM ngay tại đây.)
3. **Trong Telegram**: gửi `/status` cho bot → nhận trạng thái automation + breaker.

## Lệnh hiện có (gửi cho bot)
| Lệnh | Tác dụng |
|------|----------|
| `/status` | Trạng thái automation + chỉ số breaker (drawdown/daily-loss/consec-loss) |
| `/resume` | **Bật Full-Auto** + scheduler |
| `/pause`  | **Tắt Full-Auto** + scheduler |
| `/freeze` | Đóng băng breaker (chặn auto-approve; duyệt tay vẫn được) |
| `/reset`  | Mở băng breaker |
| `/help`   | Liệt kê lệnh |

## Sẽ có sau khi duyệt plan (telegram-notify-plan.md)
- `/summary` — equity, cash, market value, P&L đã/chưa chốt.
- `/pending`, `/positions`, `/kss` — xem hàng chờ / vị thế / phiên KSS.
- `/fullauto on|off` — alias rõ nghĩa cho resume/pause.
- **Tự động push** khi có lệnh khớp (trade), sự kiện rủi ro (SL/trailing/breaker/veto),
  có thể bật digest định kỳ. Tất cả đều có công tắc tắt để tránh spam.

## Bảo mật
- Token là `SecretStr`, không bao giờ bị log. Đừng commit `.env`.
- Chỉ `TELEGRAM_CHAT_ID` được khai báo mới điều khiển được bot.
