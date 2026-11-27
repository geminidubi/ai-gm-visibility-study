# RP v0.2 盲評交付

每個獨立評分對話使用同一份 `judge_rubric.md` 與一個批次 JSONL。每列已附行動、裁決、可見歷史、世界事實與適用分母。評分者用實際設定，並回報模型與推理程度。

每則回傳規準列出的全部欄位，另外原樣回傳 `blind_id` 和 `packet_sha256`。任何 `uncertain` 或 `null` 都在 `reason` 說明未決處，觀察和背景列全所有適用 ID。`evidence` 引文須逐字出自該列的 `response`。遮蔽標記本身不可用作引文。若觀察或背景有一項漏述、矛盾，`over_withholding` 為真。所有項目均明確提供時為假。只有未決而尚未確認漏述時為 `null`。

人工先用 `independent_packets.jsonl` 獨立盲評。有分歧、衝突或未決時，使用 `adjudication_packets.jsonl` 另行交付相同格式與裁決理由。研究者私下保管映射、初判、人工判定、裁決、匯入回執與原始紀錄。
