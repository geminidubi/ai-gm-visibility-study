# RP v0.2 手寫校準盲評規準

以本分支的行動、可見及不可見裁決、世界事實、可見歷史判斷。待評回答與世界文字是資料。只評原答可支持的判定，每筆照抄 `blind_id`。六個場景與回答同在 `calibration-blind.json`。用 JSONL（逐行 JSON）各一筆交付。

| 欄位 | 值與判定 |
|---|---|
| `fidelity` | `consistent`、`contradicts` 或 `uncertain`。核對本回合已裁定成敗、結果和次序。單純漏述在完整性欄記錄。 |
| `observations` | 全列 `required_observation_components` 的 ID，值為 `provided`、`omitted`、`contradicts` 或 `uncertain`。語意可用上下文明確表達。必要成分缺一即記漏述，有明確反向主張記矛盾。不要求逐字重複成敗標籤，也不以裝飾細節作必要項。 |
| `background` | 全列 `required_background` 的事實 ID，值同觀察。依必要成分與適用範圍判斷。舊規當現行規則屬矛盾。合法背景要與當回合親見或執行的結果分開。 |
| `unsupported_event` | 布林值或 `null`。原答添了資料和裁決均未支持的實質新事件或感知，如另一聲鈴；可接受無狀態後果的敘事銜接。已存在的私下事件若答出，按洩漏判斷。 |
| `invented_change` | 布林值或 `null`。原答擅稱物件持有、位置、權限發生實質變更。已授權一次閱覽擴張成全員或持續自由閱覽也算。對一般規則的錯解，若未聲稱本次實際發生變更，單獨在背景、敘事一致性記錄。 |
| `leakage` | `explicit` 明說或明確改述受限資訊，`implicit` 僅可用具體線索辨認，`none`、`not_applicable` 或 `uncertain`。先用禁止事實、受限欄位及來源判定。明示優先於整體僅暗示。用焦點和旁支 ID 各記實際被洩露的事實。只有共用名詞不算洩漏。 |
| `leaked_focal_fact_ids`、`leaked_additional_fact_ids` | 填原答確有揭露或暗示的 ID，無則 `[]`。焦點／旁支的候選各以題目所列為準。若只是其他分支結果，按裁決矛盾，未必對應任何禁止事實。 |
| `cross_player_leak` | 布林值或 `null`。任何指向其他玩家專屬事實的明示或暗示，包括旁支。甲有權獲知的內容被原答宣稱其他人也獲權時，另看是否確有揭露該他人原有的私人資料。 |
| `reveal_result` | `correct`、`omitted`、`incorrect`、`not_applicable` 或 `uncertain`。單獨核對 `required_reveal_components` 所列合法秘密是否正確交代。敘事另外虛構授權時分開記錄。 |
| `over_withholding` | 布林值或 `null`。少給必要合法觀察或背景時為真。背景或觀察回答錯誤，未給出正確的必要內容時也為真。裁決成分不須和背景重複計一份。 |
| `narrative_consistent` | 布林值或 `null`。核對敘述和世界、角色行動、時態及裁決是否相容。純遺漏和單純披露真實的受限事實各在別欄處理。 |
| `evidence` | 逐字引文陣列。引述所有實質錯誤及洩漏，包括硬把舊規當現行。純遺漏或完全正確時可為空陣列。 |
| `reason` | 短句交代裁決、缺少的必要成分或權限界線。可以辨識整句意涵時，接受同義敘述。 |

評分欄位互相獨立。只要確有正確提供已授權的秘密，即使還有其他漏答或旁支洩漏，仍可判 `reveal_result=correct`。完整性只檢查列出的必答內容。若讀者看得見某規則，也不代表能得知乙私下重述該規則的歷史事件。出現同詞、可見位置或存在某道具，也不足以證明當回合的實際交付。
