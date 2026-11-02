# 自建車牌辨識（vision/）

用一般攝影機＋自己訓練的模型認車牌。分四段，全部在公司電腦上跑，**照片不上傳、不用雲端、不花錢**。
這條線**只認車牌、只記錄，不會送任何開閘指令**，也不碰攝影機白名單。

| 階段 | 做什麼 | 怎麼開 |
|---|---|---|
| 1 收集 | 24 小時抓車牌照片、每天日報 | `start_collector.bat` |
| 2 標記 | 網頁看切圖、點對或改字（每天 5～10 分鐘） | `start_label.bat` |
| 3 訓練與考試 | 用標好的照片練自己的模型、每週成績單 | `weekly_train.bat`（plate_trainer） |
| 4 模擬車道 | 車停在鏡頭前，量「從車出現到確定」幾秒 | `plate_collector.exe --lane-test` |

## 安裝（公司桌機）
1. 把 `plate_collector` 和 `plate_trainer` 兩個資料夾放在**同一層**，例如 `C:\plate\plate_collector`、`C:\plate\plate_trainer`。
2. 在 `plate_collector` 裡把 `config.example.json` 複製成 `config.json`，用記事本填：
   - `camera_host`：錄影主機的 IP；`camera_password`：密碼（**只填在這台電腦，不要傳給別人、不要貼到聊天**）
   - `cameras` 裡每路的 `channel`（NVR 第幾路）。雄邁 NVR 的網址格式已經寫好，不用改。
3. 測試：在 `plate_collector` 資料夾空白處 Shift＋右鍵 →「在終端機開啟」，輸入 `.\plate_collector.exe --test-camera`。
   每路存一張 `data\test_frame_名稱.jpg`（一天後自動刪），看角度和車牌大小。

## 第 1 階段：收集（24 小時）
- **啟動**：點兩下 `start_collector.bat`；**停止**：在黑色視窗按 Ctrl+C 或關掉視窗。
- **開機自動啟動**：`Win+R` 輸入 `shell:startup`，把 `start_collector.bat` 的**捷徑**放進去。電腦睡眠要關掉。
- 有車經過就連拍最多 5 張；另外每 60 秒定時拍一張（抓停著的車、機車），同一台車一小時內只存一次。
- 沒找到車牌就不存照片；完整畫面只在確定像車牌時才存（減少拍到路人）。
- 每天日報 `data\reports\日期.txt`；照片 90 天自動刪除；硬碟剩不到 20 GB 暫停存照片。
- 狀態看黑色視窗（每分鐘一行）或 `data\status.json`。

## 第 2 階段：標記
- 點兩下 `start_label.bat`，瀏覽器會自動開 http://127.0.0.1:8790（**只有這台電腦開得到**；在外面就用遠端桌面連進來開）。
- 畫面一次一台車：上面是連拍的車牌切圖，下面是程式猜的字。
  - 字對了就按 **Enter**；不對就改成正確的字（不用打「-」）再按 Enter。
  - 切圖是別台車或不是車牌 → 取消它下面的勾。
  - **F2** 看不清楚、**F4** 不是車牌、**PageDown** 跳過、**PageUp** 回上一張改。
- 不用看的：連拍 3 張以上都認出一樣的字、信心度 0.9 以上 → 自動當答案（其中約 5% 仍抽出來給你看，用來量自動答案準不準）。
- 「匯出訓練資料」按鈕：把答案和切圖整理到 `data\dataset\`。收集程式每天也會自動匯出一次（在刪過期照片之前）。
- 訓練資料和人工答案的保存天數是 `config.json` 的 `dataset_retention_days`（預設 90，**跟照片一樣**，到期一起刪）。想讓訓練資料留得比照片久就把它設大，但那也是個資，請自己決定。

## 第 3 階段：訓練與考試
- 點兩下 `plate_trainer\weekly_train.bat`（約 3 小時，建議週末晚上）。也可以用「工作排程器」設定每週日凌晨自動跑：
  動作選「啟動程式」→ 選 `weekly_train.bat`，「開始位置」填 plate_trainer 資料夾。
- 會練兩個模型：**認字模型**（我們自己的，從「假車牌預先訓練版」接著練）、**是不是車牌過濾器**（要有 30 張以上「不是車牌」才練）。
- 練完自動考試，成績單在 `data\reports\scorecard_日期.txt`：白天／晚上分開，「全對」「認錯」「認不出來交給人」分開算，並跟現成模型比。
- **考卷**：約 20% 的車牌（照車牌分組）留作考卷，人工確認過、**永遠不拿來訓練**，同一台車天天出現也不會混進訓練資料。
- 新模型要考得比目前使用的好，才會換成「目前使用」（`data\models\current.json`），否則留著不用。
- 收集程式現在仍用現成模型（讓標記網頁的建議一致）；車道測試預設用「目前使用」的我們的模型。

## 第 4 階段：模擬管制車道
- 在 `plate_collector` 資料夾開終端機：`.\plate_collector.exe --lane-test --camera 金爐`
  （或錄一段影片：`.\plate_collector.exe --lane-test --source 影片.mp4`）。
- 測試白名單：在 `data\lane_whitelist.txt` 一行寫一個車牌（只用來測試判斷，不會寫進任何攝影機）。
- 每台車顯示：**符合白名單（模擬放行，未開閘）**／**只差一個字，不自動放行，提示人工**／**不在白名單**／**湊不到票，交給人判斷**，
  以及「從車出現到確定」幾秒。紀錄在 `data\captures\日期\lane_test.jsonl`。
- 投票規則：信心度 0.8 以下不算票、格式不符不算票、同一個字先到 3 票才確定、看到車牌後 3 秒湊不到就交給人。

## 資料放哪（全部在 data\，不進 git）
```
data\captures\日期\               照片、events.jsonl、車道測試紀錄（90 天刪）
data\labels\labels.jsonl          人工答案
data\dataset\                     訓練資料（答案＋切圖；跟人工答案一樣照 dataset_retention_days 刪，預設 90）
data\models\                      練好的模型、current.json（目前使用）
data\reports\                     每日日報、成績單（只有數字）
```
車牌照片是個資：只放這台電腦、不要複製到隨身碟或雲端硬碟。
`config.json` 有攝影機密碼：備份或壓縮資料夾給別人時，先把 `config.json` 和 `data` 拿掉。

## 授權（全部免費、可商用）
RapidOCR／PaddleOCR 模型（Apache-2.0）、OpenCV（Apache-2.0）、onnxruntime（MIT）、PyTorch（BSD）、ONNX（Apache-2.0）、NumPy（BSD）。
**沒有用 Ultralytics YOLO**（AGPL，商用要付費）。認字模型和過濾器是我們自己寫、自己練的。

## 開發用（筆電）
```
cd vision
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pip install torch==2.15.0+cpu --index-url https://download.pytorch.org/whl/cpu
.venv\Scripts\python -m pip install -r requirements-train.txt
.venv\Scripts\python synth.py --fake-captures 測試資料夾 --days 3 --events 120   產生假的收集資料測整條流程
.venv\Scripts\python train.py pretrain --minutes 45                            用假車牌預先訓練（結果在 models\）
build_exe.bat                                                                  打包兩個程式到 build_out\
```
