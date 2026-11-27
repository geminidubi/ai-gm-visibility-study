# G5 v0.2 Work 批次盲評與人工裁決

本流程把研究者的原始紀錄與答案證據組成獨立的盲評資料檔。正式語意評分使用 ChatGPT Work 的模型用量，評分程式只處理共用檔案。模型生成仍需要另外核准。開發與正式模式的生成資料會留下原答、選用嘗試及失敗紀錄。

## 準備開發批次

在 repo 根目錄用已安裝依賴的環境執行。

```powershell
$env:PYTHONUTF8 = "1"
$g5Python = ".venv/Scripts/python.exe"
& $g5Python -m ai_gm_visibility.cli g5 validate
$g5Run = & $g5Python -m ai_gm_visibility.cli run --config configs/g5-dev.json --all | ConvertFrom-Json
$g5RunId = Split-Path -Leaf $g5Run.directory
$g5Gold = "data/g5/dev_gold.jsonl"
$g5Bundle = "runs/g5-review-$g5RunId"
& $g5Python -m ai_gm_visibility.cli g5 export --run $g5RunId --gold $g5Gold --output $g5Bundle --batch-size 12 --max-batch-bytes 220000
```

以 `WORK_BLIND/` 作為共用交接資料夾。可把整個 `WORK_BLIND/` 壓縮交付，請 Work 依 `work_instructions.md` 及 `judge_rubric.md` 每次評一個 JSONL 檔。Work 回傳可下載的 UTF-8 JSONL 及其顯示的評審模型名稱。批次預設最多 12 筆或 220,000 個 JSONL 位元組，可調成 10～20 筆，也可為較長資料使用更小批次。單筆超過位元組目標時獨立成批，清單會標記。

每筆匿名 ID 只對應一個已採用的回答。批次順序在匯出時打散。Work 可見玩家問題、原答、參考答案、必要、公私與歷史事實、允許推論及適用性。原答中的顯式架構代碼會遮蔽並列為人工複查，原文仍保存在研究者端。文字風格及回答內容可能使評審推測生成架構。研究者私下保管 `mapping_PRIVATE.json`、`work_review_PRIVATE.sqlite3`、原始執行紀錄和評估輸出。

## 匯入、核對、續評

將下載的 Work 回答放在本機或已掛載的共用資料夾，再將資料夾或指定的 JSONL 路徑傳入。不同模型的回傳檔分開匯入。模型未揭露時填 `not_disclosed`。

```powershell
& $g5Python -m ai_gm_visibility.cli g5 import-work --run $g5RunId --gold $g5Gold --bundle $g5Bundle --input "runs/work-returns" --model "<Work 顯示的模型名稱>"
& $g5Python -m ai_gm_visibility.cli g5 work-progress --run $g5RunId --gold $g5Gold --bundle $g5Bundle
& $g5Python -m ai_gm_visibility.cli g5 resume-work --run $g5RunId --gold $g5Gold --bundle $g5Bundle --output "runs/work-pending-round-2"
```

- 匯入逐筆核對匿名 ID、盲包雜湊、欄位、適用事實 ID、原答逐字引文及互相依賴的判定。缺欄位、未知 ID、無法核對的引文及同一次匯入中重複的 ID 不計分。有效紀錄在同一次交易中保留。有拒收項目時回傳結束碼 3，並輸出逐行回執。
- 原始匯入檔、各行結果、模型、評審來源、規準版本與時間存入原盲包的 SQLite。重送完全相同的有效評分會辨識為 `already_accepted`。同一匿名 ID 的另一份有效判定會保留為分歧，進人工流程。
- `work-progress` 列出有效收件、漏審、拒收與未決欄位。續評以同一私有盲包為基準，只匯出未取得有效判定的 ID。已收到了格式有效但未決的判定交人工複查。
- 匯入自動檢查引文存在於回答及 ID 適用。引文是否在語意上支持該事實，仍由獨立評審及人工裁決確認。

## 人工盲評與裁決

先由獨立人工讀 `independent_packets.jsonl` 和規準，看不到 Work 原判。每架構用固定種子隨機抽至少 10% 的可評回答，另加全部未決、顯式架構遮蔽與已知分歧案例。隨機樣本的一致度與加抽分開報。匿名 ID、雜湊及回傳格式沿用 Work 階段。評完一部分可回傳分開的 JSONL，後續用同一資料夾載入先前有效的評分。

```powershell
& $g5Python -m ai_gm_visibility.cli g5 assess --run $g5RunId --gold $g5Gold --bundle $g5Bundle --output "runs/g5-independent"
& $g5Python -m ai_gm_visibility.cli g5 assess --run $g5RunId --gold $g5Gold --bundle $g5Bundle --human "runs/human-returns" --output "runs/g5-arbitration"
& $g5Python -m ai_gm_visibility.cli g5 assess --run $g5RunId --gold $g5Gold --bundle $g5Bundle --human "runs/human-returns" --adjudications "runs/adjudication-returns" --output "runs/g5-final-assessment"
```

有人工作完獨立判定後，`adjudication_packets.jsonl` 才會同行提供原判。由人工裁決者比較證據、決定最終判定，並記理由。各次 `assess` 使用新輸出目錄。保留 `decisions_PRIVATE.jsonl` 的初判、獨立人工判、裁決、採用來源與未決欄位，並用 `adopted_units_PRIVATE.jsonl` 對回預定生成單元及技術失敗。

`assessment_PRIVATE.json` 報告整體、架構、家族及題目條件的統計。適用、已評、未執行、生成失敗、缺評及未決各有計數。未填滿適用分母的指標不出具完整比率。故事家族為解讀單位。難度、問法與重複樣本用於描述結果。

## 重現本輪離線驗證

```powershell
& $g5Python scripts/build_g5.py
& $g5Python scripts/check_g5.py --output runs/g5-offline-check
& $g5Python scripts/demo_g5_work.py --output runs/g5-work-rehearsal --handoff-zip runs/g5-work-calibration.zip
& $g5Python -m unittest discover -s tests -v
```

`demo_g5_work.py` 使用手寫的示例原答、Work 格式回傳檔和人工裁決檔，執行整個檔案交接流程。選填的 ZIP 使用另建的全新匿名 ID，可交給 Work 做實際開發校準。對帳計畫保存在演練目錄下 `fresh_work_bundle/`，執行目錄是 `runs/g5-work-rehearsal/runs/g5-work-fixture`。實際匯入時將上面各命令的 `--output-root` 加為 `runs/g5-work-rehearsal/runs`，並使用這兩個位置對帳。已交付 ZIP 的實際位置見複審摘要。[正式公開與重現方式](reproduction.md)

