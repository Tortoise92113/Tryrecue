# WHV Job Tracker

一個專為打工度假（WHV）設計的澳洲職缺追蹤工具，自動從多個求職平台抓取職缺、以 Gemini AI 判斷是否 WHV 友善，並提供網頁介面瀏覽與管理。

## 功能

- 自動從 Adzuna、Backpacker Job Board、Jora 抓取職缺
- 使用 Gemini AI 判斷職缺是否接受 WHV 簽證
- 標記可集簽地區（Regional Area）的職缺
- 網頁介面支援篩選（城市、職缺類型、狀態）
- 職缺狀態管理（new / saved / applied / hidden）
- Email 通知新職缺
- 排程自動執行（每日定時抓取）

## 安裝

```bash
pip install -r whv-job-tracker/requirements.txt
playwright install chromium
```

## 設定

複製範本並填入你的設定：

```bash
cp whv-job-tracker/config.example.yml whv-job-tracker/config.yml
```

編輯 `config.yml`，填入以下資訊：

| 欄位 | 說明 |
|------|------|
| `adzuna.app_id` / `app_key` | [Adzuna API](https://developer.adzuna.com/) 申請 |
| `gemini.api_key` | [Google AI Studio](https://aistudio.google.com/) 申請 |
| `email.sender` | Gmail 帳號 |
| `email.app_password` | Gmail 應用程式密碼（需開啟兩步驟驗證） |
| `search.cities` | 要搜尋的澳洲城市列表 |

## 執行

**手動執行一次（抓取 + 分類）：**

```bash
cd whv-job-tracker
python main.py
```

**啟動網頁介面：**

```bash
cd whv-job-tracker/web
python app.py
```

瀏覽器開啟 `http://localhost:5000`

**排程自動執行：**

```bash
cd whv-job-tracker
python run_scheduler.py
```

## 專案結構

```
whv-job-tracker/
├── main.py              # 主程式（抓取 + 分類）
├── config.yml           # 個人設定（不納入版控）
├── config.example.yml   # 設定範本
├── storage.py           # SQLite 資料庫操作
├── classifier.py        # Gemini AI 分類
├── notifier.py          # Email 通知
├── run_scheduler.py     # 排程執行
├── sources/             # 各平台抓取器
│   ├── adzuna.py
│   ├── backpacker_job_board.py
│   └── jora.py
└── web/
    ├── app.py           # Flask 網頁後端
    └── templates/       # 網頁前端
```
