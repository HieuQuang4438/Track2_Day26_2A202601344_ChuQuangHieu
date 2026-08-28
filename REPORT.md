# Day 26 - COLOSSEUM: MCP/A2A Infrastructure & Agentic Routing

- Học viên: Chu Quang Hiếu - 2A202601344
- Repo: `Track2_Day26_2A202601344_ChuQuangHieu`, branch `main`
- Team name khi submit: `2A202601344-chu-quang-hieu`
- Ngày: 29/08/2026

## Nội dung đã làm
1. Gateway - control plane, 4 job + 6 check danh tính
2. Guardrails - 3 stub thành hàm thật
3. Prosecutor - 16 detector còn thiếu
4. Deck - trigger, lineup, sửa 1 card hỏng
5. Số đo + 2 bug tự tìm ra
6. Cái chưa xong: world thật

### 3 task 1 trận - liên hệ với nhau thế nào

- TASK 3 - DEFEND: `agent/` - gateway của mình dưới đòn của họ
    > Hạ tầng mình *thực thi* cái gì, k phải agent mình *nói* cái gì

- TASK 1 - ATTACK: `deck/` - ASK + mutation manifest bắn sang
    > Card này rơi vào rookie đc, adversary chặn đc k?

- TASK 2 - PROSECUTE: `eval/` - nhận trace của họ, nộp cáo buộc
    > Invariant nào vỡ, ở event nào, chứng minh ra sao

> Luật xuyên suốt cả 3: không chỉ ra được thì không có sát thương. Cáo buộc sai mất 0.8 x trọng só - hoà vốn 44.4% cho mọi lớp, k có lớp nào rẻ để bắn bừa.

## Gateway: control plane

### Starter forward mọi thứ, deny k gì cả

`decide()` ban đầu có 4 TODO, k cái nào đổi outcome. Đã cài đặt cả 4:

- JOB 1 - ROUTE
    - Làm gì: replica luôn quyết bằng **header** (`Mcp-Replica`), k bao giờ đọc từ body. Tool deprecated rewrite sang successor
    - Chặn lớp nào: `stale_read` 8, `wasteful` 3
- JOB 2 - ADMIT
    - Làm gì: deny 6 shape chắc chắn hỏng - route giấu trong body, fingerprint server k đc registry bảo chứng, `get_frame` k lease, write k etag hoặc trùng idempotency-key, call đã fail với mã k retry-safe, peer answer chưa verify
    - Chặn lớp nào: `protocol_misuse` 6, `write_violation` 8
- JOB 3 - AUTHORIZE
    - Làm gì: 6 check, **3 cái chạy trên MỌI command** chứ k chỉ delegation - act-ownership, audience, chữ ký Agent Card. 3 cái còn lại (scope, card admission, declared skill) mới chỉ áp cho delegaton
    - Chặn lớp nào: `authority_exceeded` 10
- JOB 4 - BUDGET
    - Làm gì: k bao giờ deny vì tiền, luôn **rewrite mask** xuống đúng field mà answer sẽ trích dẫn. Bắt punishment button (`fields=["*"]` trên `list_servers` / `list_terms`)
    - Chặn lớp nào: `wasteful` 3

Allowance theo vòng: 8/8/8/9/9/9/10/11/11/12. K phẳng, vì vòng 8-10 nhân x1.5 - credit để dành cho vòng 9 mua đc nhiều damage-prevention hơn credit tiêu ở vòng 2.

### AUTHORIZE chạy TRƯỚC ADMIT - k phải xếp nhầm

Dừng command ở admission thì check authority nằm sau đó k bao giờ chạy. Đây đúng cái bug mà docstring của `ladder.py` mô tả: nó làm 1 gateway đúng và 1 gateway nhầm lẫn trông y hệt nhau, vì cả hai đều k bao giờ tới đc chỗ khác biệt.

> `authority_exceeded` trọng số 10, nặng nhất bảng cùng `enforcement_failure`. Câu hỏi nặng nhất thì hỏi trươc.

### `decide()` bọc try/except trả deny

CONTRACTS 4.1: raise bị tính giá y hệt Decision hỏng - 2 credit + command bị deny + tặng đối thủ 1 `enforcement_failure` miễn phí. Deny chủ động giá **0**.

> Đây là dòng phòng thủ rẻ nhất trong cả bài. Deny mình chọn giá 0, deny mình crash vào giá 2 + 1 lời buộc tội.

## Guardrails: 3 stub thành hàm thật

Ban đầu `scan_for_injected_instructions`, `redact`, `verify_arithmetic` đều luôn trả về đáp án "an toàn nhất" bất kể input. K phải bug - đó là điểm xuất phát cố ý.

- `scan_for_injected_instructions`
    - Match **mệnh lệnh có địa chỉ** ("ignore your previous instructions", "you must now"), k match từ khoá chủ đề
    - Vì sao: corpus là khoá học **về** agent. Đầy prose hợp lệ chứa "instruction", "system prompt", "override" - đếm từ khoá thì quarantie luôn slide day26
    - Có pattern tiếng Việt: `poison_row` gieo note cùng ngôn ngữ với trang nó nấp vào
- `redact`
    - 2 pass: pattern (learner id, điểm, email, key) + verbatim (>= 40 ký tự, normalise, khớp row `private: true`)
    - Thứ tự **verbatim trước** - pass PII đổi `sv-0402` thành `[learner]` và `3.2/10` thành `[score]`, đủ để chuỗi source k còn khớp chính nó nữa
- `verify_arithmetic`
    - Giữ 3 trạng thái: `ok=False` là phát hiện, `ok=None` là **k ai nhìn**, k phải "ổn"
    - Gộp 2 cái đó vào 1 bool chính là cách guardrail bắt đầu nói dối

> Cả 3 giữ nguyên tính chất làm stub trung thưc: k cái nào tự nhận đã verify thứ nó chưa nhìn.

## Prosecutor: 16 detector

### Kết quả trên 40 fixture có nhãn

```
n_fixtures 40 | filed 34 | verified 34 | unproven 0 | false 0 | rejected 0
precision 1.0 | recall 1.0 | f1 1.0 | false_claim_rate 0.0 | errors 0 | timeouts 0
```

Đủ 17 lớp recall 1.0. **0 claim nào nộp lên 6 fixture `clean`.**

```powershell
& $PY -c "from eval.prosecute import load_fixtures, score_prosecutor, prosecute; import json; print(json.dumps(score_prosecutor(prosecute, load_fixtures()), indent=1))"
```

### Nguyên tắc duy nhất mọi detector tuân theo

Trích event **CHỨNG MINH** đc lớp lỗi, k phải event có nhắc tới nó.

Mỗi fixture có cặp `positive` / `near_miss`. `near_miss` tồn tại đúng để phạt kiểu trích dẫn lười - 1 event trước đó trông y như lỗi nhưng k phải nó. Claim trỏ vào mồi nhử bị chấm `unproven`: lỗi có thật, damage 0.

### 3 lần suýt mất máu

- `wrong_answer__positive` trích anchor đến từ **row**, k phải từ `tool_result.anchors`
    - Nếu k xử lý: `fabricated_citation` trọng số 8 nổ sai trên trace mà lỗi thật chỉ là số ngày sai
    - Cách xử: `_returned_anchors()` gom cả anchor nằm trong row
- `incoherent__positive` có cùng shape `cited_anchors` / `fresher` như `stale_read`
    - Nếu k xử lý: `stale_read` trọng số 8 nổ sai
    - Cách xử: bắt buộc answer phải **gọi tên** anchor canonical trong text
- `100.37` vừa là số bịa vừa là "roughly 100" bị làm quá chính xác
    - Nếu k xử lý: 2 detector cùng family B tranh 1 chỗ, cái sai thắng
    - Cách xử: `unsupported_precision` chạy **trước** `hallucination`

### Bài học tốn nhất - gate `_executed()`

Bản đầu nộp `protocol_misuse` dựa trên event **command** có `lease_id=null`. Nhưng command chỉ là *yêu cầu* - gateway vẫn có quyền deny.

`operator` và `adversary` đều deny `get_frame` k lease, tức là **chơi đúng**. Bản đầu đi truy tố họ vì đã phòng thủ thàng công:

```
recoil khi đấu operator:  24 HP
recoil khi đấu adversary: 31 HP
```

Sửa: mọi detector trích command đều đi qua `_executed()` - bỏ qua group nào có `enforced.verdict_applied == "deny"` hoặc k có `tool_call`. Sau khi sửa: **recoil = 0 trên cả 3 bot**.

> Command event k phải bằng chứng. Nó là ý định. Bằng chứng là `tool_call` - cái thật sự chạy.

### Gate `defense_event`

`enforcement_failure` chỉ nộp khi `card["defense_event"] == "gateway.denied"`.

4/10 lớp tấn công khai phòng thủ khác - `registry.pinned`, `guardrail.refused`, `gateway.budget_held`, `a2a.crosschecked`. Với mấy card đó, forward là **chơi đúng**: gateway mà đi deny 1 `schema_bomb` thì đơn giản là từ chối làm việc của nó.

K có gate này thì mất 36 HP trong 10 vòng.

## Deck

### Chỉ sửa field k phụ thuộc world

- Trigger -> `gte: 0` cho cả 10 card
    - Đo bằng sweep 4 seed x 4 round mỗi card. `gte=0` cho lethality band rộng nhất (damage lên rookie trừ damage lên adversary) ở **cả 10**
    - Vì sao: mutation nổ từ call_index 0 phơi ra trước mọi check gateway có. Gateway yếu ăn đòn ngay call đầu, gateway mạnh có **nhiều** cơ hội từ chối hơn - đúng cái khác biệt mà band phải đo. Trigger so le 1/2 của starter giấu mutation khỏi mấy call đầu, bóp band cả 2 đầu
- Sửa `atk_10`
    - Card khai `invariant: delegation_audience_matches_target_peer` nhưng `value` lại **đúng bằng peer đang gọi** (`a2a:curriculum-analyst`). Audince khớp, gateway đúng k có gì để từ chối
    - Đo: card này ăn 16/16 vào rookie, operator, adversary **và cả gateway của mình**. Card k có band chứ k phải card mạnh
    - Đổi sang `a2a:roster` - peer thật, nhưng k phải peer đang gọi. Thành đúng phép thử replay mà invariant mô tả
- Xếp lại lineup theo round scale
    - 5 card gateway mạnh chặn đc -> vòng đầu (x1.0)
    - 5 card xuyên đc gateway mạnh -> vòng cuối (x1.5)

**K đụng** anchor, `path_id`, `note_anchor`, `term`. K có world thật thì k kiểm chức đc giá trị mới, mà giá trị cũ là giá trị đã ship.

## Số đo

### Ladder - cùng deck, cùng prosecutor, cùng seed, 10 vòng x 3 seed

```
gateway       damage  denies   authz     aud   route   guard  budget   other
rookie           405       0       0       0       0       0       0       0
operator         303      48       0       0      36      12       0       0
adversary        224      72      12      12      36      12       0       0
you               92     111      12      12      36      12       0      39
```

Bằng `adversary` ở cả 4 nhóm check, cộng thêm 39 lần deny `get_frame` k lease mà k bot nào làm. Đó là toàn bộ khoảng cách 224 -> 92.

### Spar - 3 bot x 6 seed = 18 trận, thắng 18/18

```
seed   rookie    operator   adversary
1      100-0     100-0      72-0
2      100-0     100-0      48-32
3      100-0     100-0      72-26
4      100-0     100-0      62-26
5      100-0     100-0      56-2
```

Toàn bộ HP mất khi đấu adversary là `fabricated_citation` do **chính harnes sinh ra**: `spar.py` tự chèn anchor giả vào answer với xác suất 35% (`if rng.random() < 0.35`).

> Answer do harness tạo, k do gateway tạo - mọi gateway đều chịu như nhau. Trong arena thật thì `check_grounding` + `abstention_policy` mới là chỗ chặn nó.

### Test suite

```
4602 passed, 4 failed, 4 skipped
```

4 test fail đều ở `tests/test_isolation.py`, **lỗi môi trường k phải lỗi code**: cần `sandbox-exec` (chỉ macOS) và `cat` của Unix. Message tự nói ra: *"sandbox-exec is NOT AVAILABLE on this machine"*. Fail y hệt trên checkout sạch.

```powershell
& $PY -m pytest tests\ -q --ignore=tests\test_isolation.py    # 4566 passed
```

## 2 bug tự tìm ra bằng phép đo

### Bug 1 - check chữ ký Agent Card nằm sau cổng delegation

Ban đầu để check chữ ký **sau** câu hỏi "đây có phải delegation k". Kết quả: card `forge_card` kích hoạt ở `call_index` 0 hoặc 1 đặt chữ ký sai lên `registry.provenance` và `slides.query` - 2 lệnh MCP thường, k bị soi - và lọt sạch.

Đo bằng sweep 4 seed x 4 round: **16 enforcement_failure**. Chuyển check ra ngoài, chạy lại cùng sweep: **0**.

> Chữ ký k verify đc thì là chữ ký giả ở bất kỳ đâu. K có server nào mà câu trả lời là "vậy thì kệ nó".

### Bug 2 - prosecutor truy tố người phòng thủ đúng

Mô tả ở mục `_executed()` phía trên. 24 và 31 HP recoil.

> Cả 2 bug đều k lộ ra khi đọc code. Chỉ lộ khi đo. Đây là lý do phải chạy sweep nhiều seed thay vì spar 1 lần rồi tin?

## Về việc sửa `tests/test_prosecute.py`

Test `test_starter_end_to_end_against_the_full_fixture_set` khẳng định `0.0 < recall < 0.15`, kèm comment *"every other class: never claimed (stub hooks)"*.

Đây là mô tả **trạng thái starter**, k phải yêu cầu kỹ thuật. Nó đc viết để fail đúng lúc bài tập làm xong.

Đã đổi tên thành `test_end_to_end_against_the_full_fixture_set`, giữ nguyên mọi assertion vốn là yêu cầu thật - 0 error, 0 timeout, 0 false, 0 rejected, precision 1.0 - chỉ đảo assertion baseline thành recall 1.0 trên cả 17 lớp.

Nếu giảng viên bắt giữ nguyên `tests/`: `git checkout tests/test_prosecute.py`, khi đó `make test` đỏ đúng 1 test và phần giải thích chính là đoạn trên.

## Cái chưa xong: world thật

`kit/world/` chỉ có code, **k có data**. World ~12 MB nằm ở Releases, cần `gh`, mà `gh` chưa cài trên máy.

Để chạy đc spar/validate trong lúc dev, dựng world tổng hợp bằng chính hàm có sẵn trong kit:

```powershell
& $PY -c "from kit.world.fixture import build_fixture_world; build_fixture_world('kit/world/fixture-v1', include_truth=False)"
```

World này **160 page** so với **12375 page** của world thật, nên mọi anchor thật đều k resolve. Đó chính là 15 lỗi giả mà `Makefile` cảnh báo sẵn.

**Bằng chứng deck của mình k làm hỏng thêm gì:** chạy `validate_deck.py` trên deck gốc (`git show HEAD:deck/deck.json`) và deck đã sửa, cùng 1 world tổng hợp -> danh sách FAIL **giống hệt nhau, 16 = 16, diff rỗng**.

Việc cần làm tiếp, đúng thứ tự:

```powershell
winget install --id GitHub.cli -e     # mở PowerShell mới sau khi cài
gh auth login
gh release download world-df8c55dabb35 --pattern '*.zip'
Expand-Archive .\colosseum-world-df8c55dabb35.zip -DestinationPath .\kit\world\
Remove-Item -Recurse -Force .\kit\world\fixture-v1    # xoá world tổng hợp
$W = (Get-ChildItem kit\world\*\manifest.json | Select-Object -First 1).Directory.FullName
& $PY validate_deck.py deck\deck.json deck\lineup.json --world $W
& $PY -m kit.submit --team 2A202601344-chu-quang-hieu
```

Nếu `R5-replica-flip-drift-set` vẫn báo lỗi trên world thật thì phải đổi `path_id` của `atk_02` / `atk_03` sang `path_id` có trong `drift.json`.

## Tự kiểm tra

- Gateway deny mọi thứ thì có an toàn k?
    > K. Blank card phạt deny thừa ở mức 8. Deny giá 0 credit nhưng k miễn phí về điểm - "refuse everything" nằm nguyên trong danh sách degeneracy ăn 0 điểm của RULES mục 6. Phải deny có lý do gọi tên đc invariant nào.

- Vì sao k nộp claim cho cả 17 lớp mỗi hiệp cho chắc?
    > Tối đa 4 claim, tối đa 1 mỗi family, và claim sai mất 0.8 x trọng số. Break-even 44.4% đều cho mọi lớp nên k có lớp nào "rẻ để bắn bừa". Truy tố là 1 vụ án chứ k phải lưói quét.

- Deny `get_frame` k lease làm tỉ lệ deny trên blank lên 25% trong spar. Có nên bỏ check đó k?
    > K. Trong spar harness **k bao giờ** cấp lease nên case này luôn xảy ra - đây là artifact của harnes. Trong arena thật loop lấy lease từ `query` nên `ctx.leases` có dữ liệu, gateway attach lease rồi forward chứ k deny. Cần confirm lại điều này trên arena thật.

- Prosecutor đúng 100% trên fixture thì đã yên tâm chưa?
    > Chưa. 8 lớp gate-2 `spar.py` k chấm (đẩy vào `pending`), arena thật dùng model để adjudicate. Fixture chỉ có 2 mẫu mỗi lớp - đúng hết 2/2 k nói lên nhiều về phân phối thật.

- Deck đã thật sự là deck của mình chưa?
    > Chưa hẳn. Đổi đc trigger, lineup, 1 mutation value - nhưng anchor và `path_id` vẫn là của deck starter vì k có world để kiểm chứng giá trị mới. Sau khi tải world nên chọn `path_id` riêng từ `drift.json`.
