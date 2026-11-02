# 車牌照片收集程式（自建車牌辨識 第 1 階段）

24 小時看公司門口的攝影機，收集車牌照片，給之後訓練自己的辨識模型用。
**只記錄、不開閘**：程式只讀攝影機畫面，不送任何指令給攝影機或閘門，也不碰白名單。
照片只存在這台電腦，不上傳任何雲端。

## 它做什麼
- **有車經過**（畫面有動靜）：連拍最多 5 張，找出車牌、切出來存。
- **定時拍照**（預設每 60 秒）：抓停著的車和機車。同一台車一小時內只存一次。
- 每張先用開源模型猜車牌文字和信心度（RapidOCR／PaddleOCR 模型，Apache-2.0，可商用、免費）。
- **沒找到車牌就不存照片**（路人、樹影只記「有動靜」一筆）。
- 每天自動產生日報 `data/reports/日期.txt`：車數、白天／晚上、信心度分布、各攝影機。
- 照片**超過 90 天自動刪除**（`retention_days` 可改）；硬碟剩不到 20 GB 時暫停存照片。

## 第一次安裝（公司桌機）
1. 把整個 `plate_collector` 資料夾複製到公司桌機（例如 `C:\plate_collector`）。
2. 把 `config.example.json` 複製一份，改名成 `config.json`，用記事本打開填：
   - `camera_host`：錄影主機（或攝影機）的 IP
   - `camera_user`／`camera_password`：帳號密碼（**只填在這台電腦的 config.json，不要傳給別人、不要貼到聊天**）
   - `cameras` 裡每支攝影機的 `rtsp_path`（第幾路）
3. 先測試：在資料夾空白處按住 Shift＋右鍵 →「在終端機開啟」，輸入
   `.\plate_collector.exe --test-camera`
   每支攝影機會存一張 `data\test_frame_名稱.jpg`，看角度、車牌夠不夠大（看完刪掉）。

## 啟動與停止
- **啟動**：點兩下 `start_collector.bat`。會開一個黑色視窗，每分鐘顯示一行狀態。
- **停止**：在那個視窗按 `Ctrl+C`（或直接關掉視窗）。
- **開機自動啟動**：按 `Win+R` 輸入 `shell:startup`，把 `start_collector.bat` 的**捷徑**放進去。
- 電腦的「睡眠」要關掉（設定 → 系統 → 電源 → 睡眠：永不）。
- 不小心點兩次也沒關係，第二個會自己說「已經在跑了」然後結束。

## 怎麼知道有在跑
- 黑色視窗每分鐘一行：各攝影機有沒有連線（✓／✗）、今天抓到幾次、硬碟剩多少。
- `data\status.json`：最後更新時間，超過幾分鐘沒更新就是停了。
- `data\reports\`：每天一份日報。
- `data\collector.log`：執行紀錄（不寫車牌文字）。

## 資料放哪
```
data\captures\2026-11-05\events.jsonl         每次拍照一行（時間、攝影機、白天晚上、猜的字、信心度）
data\captures\2026-11-05\顧車\153012_0007\    車牌切圖 f*.jpg ＋ 一張完整畫面 full.jpg
data\reports\2026-11-05.txt                   每日統計（只有數字）
```
車牌照片是個資：只放這台電腦、不要複製到隨身碟或雲端硬碟、不進 git。
`config.json` 有攝影機密碼：備份或壓縮這個資料夾傳給別人時，先把 `config.json` 和 `data` 拿掉。
完整畫面（full.jpg）只在認出的字符合車牌格式、信心度夠時才存，減少拍到路人。

## 開發用（筆電）
```
cd vision
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python collector.py --source 測試影片.mp4     用影片測試
.venv\Scripts\python collector.py --image 照片.jpg          認一張照片
.venv\Scripts\python stats.py 2026-11-05                    重新產生某天日報
build_exe.bat                                              打包成 build_out\plate_collector\
```
