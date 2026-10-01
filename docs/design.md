# 設計說明

## 需求對照

| 需求 | 實作 |
|---|---|
| 被變更的設備物件化 | `Device`（網路 / Windows / Linux / 其他），變更系統本身不分網路或主機變更 |
| 操作步驟日誌化 | `StepExecution`（每台設備每個步驟的結果、輸出、執行者）+ append-only `Event` |
| 設備與步驟掛勾 | `StepExecution` 存在即代表「此步驟要在此設備上執行」；草稿時在矩陣上切換 |
| 步驟模組化、明確定義可否省略 | `StepModule`（積木）+ `ChangeTemplate`（變更目的）；省略規則 `REQUIRED / OPTIONAL / CONDITIONAL` |
| 變更完畢回到變更請求更新紀錄 | 狀態 `EXECUTED` 只能經由網頁「回填變更紀錄」進入 `CLOSED` |
| 全部在網頁完成、海聊只更新基礎狀態 | 聊天只允許 `status / start / pause / resume / finish / comment` |
| RBAC | 角色 + 設備類型 / 環境範圍；申請人不能核准自己的變更 |
| 多設備互不知道彼此 | 設備與變更唯一的關聯是 `ChangeTarget`；設備頁的歷史是反查出來的 |
| 樂高積木與變形標準模組 | 計畫中可微調步驟，結案後審核：保留一次性 / 晉升為變形模組（A01 → A01-2）/ 併入母模組新版本 |
| 以時間、設備名稱、IP、變更項目、目的搜尋 | `/changes` 搜尋；IP 支援網段，並同時比對變更當下快照與目前設備資料 |

## 對 ChatGPT 規劃的調整

方向（CR → Target → Plan → Module → Execution → Review、Definition 與 Execution 分離、模組版本不可變、append-only 日誌、先做單體 + PostgreSQL）都採用。以下是不同之處與原因：

1. **積木的單位是「步驟」，不是「一組步驟」。**
   你的描述裡 A01 同時是「目的 A 的第 1 步」也是「標準模組 A01 → A01-2」，代表變形發生在步驟層級。
   所以 `StepModule` 是單一步驟，`ChangeTemplate`（目的 A）把多個 `StepModule` 組起來。
   若以 ChatGPT 的「模組 = 一組步驟」來做，改其中一步就得 fork 整組，變形數量會爆炸。

2. **省略規則屬於「組裝」，不屬於模組。**
   同一個「備份設定」步驟，在 A 目的是必要、在 B 目的可能可省略。所以 `skip_policy` 放在 `TemplateItem` / `PlanStep`，不放在 `StepModule`。

3. **微調不在計畫當下 fork，而是在結案後才決定。**
   ChatGPT 讓工程師在計畫時就建立 A01-2 候選模組，現場的一次性調整也會變成模組。
   這裡改為：`PlanStep` 是模組的快照，內容偏離來源版本時標記 `is_modified` 並必填原因；結案後由模組核准者決定保留、晉升或併入。
   併入母模組時若母模組在這段期間已有新版本會被拒絕，避免覆蓋別人的修改。

4. **狀態與結果分開。**
   ChatGPT 的 12 個狀態把「流程進度」和「結果」（`COMPLETED_WITH_ISSUE`、`ROLLED_BACK`、`FAILED`）混在一起。
   這裡狀態只表示流程，結果（`SUCCESS / PARTIAL / FAILED / ROLLED_BACK`）是回填紀錄的欄位，狀態機因此單純且無歧義。

5. **ChangeTarget 保存設備快照。**
   設備日後改 IP 或主機名，舊變更仍要能用當時的 IP 搜到。ChatGPT 的模型只有 `Device` 參照，資料會隨設備更新而失真。

6. **CONDITIONAL 不做條件運算式。**
   ChatGPT 的 `device.role == "access-switch"` 需要運算式引擎，有注入與維護成本。
   這裡用模組的「適用設備類型」自動掛勾，條件式步驟則由執行者判斷、省略時必填原因，由日誌留下證據。

7. **聊天身分與 RBAC。**
   ChatGPT 沒有處理「聊天室裡的人是誰」。這裡由系統管理員綁定 `chat_user_id`，聊天指令與網頁走同一套權限檢查；請求以 HMAC 簽章 + 時間戳驗證。

8. **不需要 OpenSearch。** 搜尋條件都是結構化欄位，PostgreSQL 的 `inet <<=` 與 `ILIKE` 已足夠；資料量成長後再加 `pg_trgm` 索引即可。

9. **append-only 由資料庫強制。** `events` 與 `step_module_revisions` 以 trigger 禁止 UPDATE / DELETE，而不只是程式慣例。

## 狀態機

```
DRAFT ──submit──▶ SUBMITTED ──approve──▶ APPROVED ──start──▶ IN_PROGRESS ──finish──▶ EXECUTED ──回填紀錄──▶ CLOSED
  ▲                  │                      │                  │    ▲
  └─────reject───────┘                      │             pause│    │resume
                                            │                  ▼    │
DRAFT / SUBMITTED / APPROVED ──cancel──▶ CANCELLED              PAUSED
```

| 動作 | 誰 | 條件 | 聊天可用 |
|---|---|---|---|
| submit | 申請人本人 | 有時段、設備、步驟；每個步驟至少掛一台設備，每台設備至少一個步驟 | 否 |
| approve / reject | 核准者，範圍涵蓋所有設備，且不是申請人 | reject 必填原因 | 否 |
| start | 執行者，範圍涵蓋任一設備 | 目前時間在核准時段內 | 是 |
| pause / resume | 同上 | | 是 |
| finish | 同上 | 沒有「待執行」的步驟 | 是 |
| 回填紀錄並結案 | 申請人本人 | 結果、驗證結果必填 | 否 |
| cancel | 申請人本人 | 必填原因 | 否 |

步驟更新（網頁）：執行者的範圍必須涵蓋該設備；`REQUIRED` 不可省略，`CONDITIONAL` 省略必填原因。

## RBAC

| 角色 | 權限 |
|---|---|
| 申請人 REQUESTER | 建立變更、編輯自己的草稿、回填紀錄 |
| 核准者 APPROVER | 核准 / 退回（受範圍限制） |
| 執行者 OPERATOR | 開始、暫停、更新步驟、執行完畢（受範圍限制） |
| 模組編輯 MODULE_EDITOR | 建立候選模組、修訂候選模組、維護變更範本 |
| 模組核准 MODULE_APPROVER | 上述 + 核准 / 停用模組、修訂標準模組、審核現場調整 |
| 稽核 AUDITOR | 檢視全系統稽核紀錄 |
| 系統管理 ADMIN | 使用者、角色、設備；**不含**變更核准與執行（職責分離） |

範圍：每筆角色指派可限定 `device_type` 與 `environment`（空白 = 不限）。核准需涵蓋變更中的所有設備；執行只需涵蓋要操作的那台設備。

## 已知限制與後續

- 設備只有單一管理 IP；多 IP 需另建 `device_ips`。
- 聊天整合只有「收指令 → 回覆」，尚無主動通知（例如核准後通知執行者）。
- 時間戳 5 分鐘內的重送不會被擋（狀態機會擋重複轉換，但 comment 可能重複）；需要時再加 nonce 表。
- 登入沒有失敗次數限制；正式上線前建議由反向代理限流，或接 SSO。
- DB trigger 擋不住資料表擁有者停用 trigger；正式環境應讓應用程式以非擁有者角色連線。
- 指令範本中的 `{{ 變數 }}` 目前只是文字，尚未做參數化代入與自動執行（Ansible / PowerShell / SSH）。
