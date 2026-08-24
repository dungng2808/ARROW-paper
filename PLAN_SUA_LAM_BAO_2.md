# Kế hoạch hoàn thiện `Làm báo (3).docx` theo `reviewer.txt`

## Cách sử dụng kế hoạch

Không ai có thể bảo đảm tuyệt đối quyết định “PASS 100%” vì quyết định cuối cùng thuộc ban biên tập. Tuy nhiên, checklist này bao phủ toàn bộ nhận xét trong `reviewer.txt` và phân biệt rõ:

- **Bắt buộc:** phải sửa trước khi nộp.
- **Đã đạt:** không cần sửa thêm, chỉ kiểm tra không làm sai lại.
- **Tăng độ chắc chắn:** reviewer không bắt buộc chạy lại toàn bộ, nhưng thực hiện thêm sẽ làm phản hồi mạnh hơn.

Mọi đoạn tiếng Anh bên dưới là nội dung để đưa vào bài. Bản tiếng Việt chỉ để đọc hiểu, không chèn vào bản tiếng Anh.

---

## Trạng thái đối chiếu toàn bộ reviewer

| Nhận xét reviewer | Trạng thái trong `Làm báo (3).docx` | Hành động còn lại |
|---|---|---|
| Làm rõ đóng góp của từng component/ablation | Đã đạt mức tối thiểu | Giữ RQ1 tắt repair; RQ2 No/Fixed/Adaptive; giữ limitation chưa ablate rollback và error signature. |
| Quality chỉ tính trên valid tests gây selection bias | Đã đạt | Giữ nội dung trong Abstract, RQ3 và Limitations. |
| Human-written tests vẫn tốt hơn | Đã đạt | Giữ kết luận ARROW bổ trợ, không thay thế. |
| Semantic correctness/oracle quality | Đã thừa nhận; chưa audit | Không tuyên bố semantic correctness đã được chứng minh. Manual audit là bước tăng độ chắc chắn, không phải chạy lại toàn bộ. |
| Chuẩn hóa vai trò GPT-4/Qwen | Đã đạt | GPT-4 là proprietary baseline; Qwen là open, self-hosted experimental configuration. |
| Inference cost, flaky tests, CI/CD | Đã đạt | Giữ trong Limitations/Future Work. |
| Data leakage sample-level/repository-level | Đã đạt về nội dung | Bảo đảm có bằng chứng nội bộ cho phép intersection bằng 0. |
| Adaptive Repair 920 giây quá cao | Đã đạt | Số mới: Fixed `350 s [325–375]`, Adaptive `200 s [180–220]`, giảm `42.86%`. |
| Coverage/mutation bị phóng đại do lọc valid tests | Đã đạt | Giữ cách diễn giải conditional, không gọi là end-to-end quality. |
| Coverage cao nhưng Mutation Score thấp; cần phân tích/assertion prompt | Gần đạt | Đã phân tích và đề xuất prompt mới; cần giữ câu nói prompt mới chưa được đánh giá. Có thể làm thí nghiệm nhỏ nếu muốn claim improvement. |
| Thiếu EvoSuite test-smell baseline | Đã đạt về số liệu | Chỉ còn sửa nhãn `Prompt` của EvoSuite và kiểm tra nhất quán. |

---

# PHASE 1 — Các việc bắt buộc còn lại trong `Làm báo (3).docx`

## 1.1. Hoàn thiện `Answer to RQ3`

Đoạn hiện tại đúng về nội dung nhưng câu cuối có thể viết gọn và chính xác hơn. Thay toàn bộ đoạn dưới heading `Answer to RQ3` bằng:

### Bản tiếng Anh để đưa vào bài

> The results demonstrate that ARROW can assess and distinguish generated-test quality across multiple dimensions. Among the valid LLM-generated tests, zero-shot-project-aware generally achieved the strongest coverage, Mutation Score, and test-smell results. Human-written tests retained the strongest fault-detection and structural quality. EvoSuite exhibited a substantially higher Smell Density and no smell-free test suites within its valid subset. Because the quality metrics were conditional on valid tests and the number of valid tests differed across configurations, these results should be interpreted as conditional quality comparisons rather than end-to-end rankings. Therefore, the findings support ARROW as a complementary automated test-generation workflow but do not justify replacing developer-written tests or claiming that ARROW universally outperforms EvoSuite.

### Bản dịch tiếng Việt để đọc hiểu

> Kết quả cho thấy ARROW có thể đánh giá và phân biệt chất lượng test sinh tự động trên nhiều khía cạnh. Trong số các test hợp lệ do LLM sinh, zero-shot-project-aware nhìn chung đạt kết quả tốt nhất về coverage, Mutation Score và test smell. Test do con người viết vẫn có khả năng phát hiện lỗi và chất lượng cấu trúc tốt nhất. EvoSuite có Smell Density cao hơn đáng kể và không có test suite nào hoàn toàn không có smell trong tập hợp lệ. Vì các chỉ số chất lượng chỉ được tính trên valid tests và số valid tests khác nhau giữa các cấu hình, các kết quả này nên được xem là so sánh chất lượng có điều kiện, không phải bảng xếp hạng end-to-end. Do đó, kết quả ủng hộ ARROW như một workflow sinh test tự động có tính bổ trợ, nhưng không cho phép kết luận rằng generated tests có thể thay thế test do lập trình viên viết hoặc ARROW luôn vượt trội EvoSuite.

Xóa đoạn ngay sau đó:

> Overall, Zero-shot-project-aware improved initial test generation, Adaptive Repair repaired failures more effectively than Fixed Repair with fewer attempts and less time, and the combined use of coverage, mutation testing, and test-smell analysis enabled a multidimensional evaluation of test quality. Nevertheless, the generated tests did not fully replace human-written tests.

Lý do: đoạn này tổng hợp cả RQ1–RQ3, lặp với Conclusion và cụm `did not fully replace` có thể bị hiểu là generated tests đã thay thế human tests ở một mức độ nào đó.

## 1.2. Sửa nhãn EvoSuite trong bảng Test Smell

Bảng đã có đúng số liệu:

- Valid tests: `30`
- Smell Density: `48.43`
- Smell-free Tests: `0 (0.00%)`

Chỉ còn đổi ô `Prompt` từ dấu `-` thành:

`Search-based baseline`

Hàng cuối phải là:

| Model | Prompt | Valid tests | Smell Density | Smell-free Tests |
|---|---|---:|---:|---:|
| EvoSuite | Search-based baseline | 30 | 48.43 | 0 (0.00%) |

## 1.3. Xuất lại Figure 3 của RQ2 ở chất lượng cao

Việc dùng hình thay bảng là chấp nhận được. Bản `(3)` hiện đã có Figure 3 thật và Figure 4 coverage/mutation được đánh số đúng. Tuy nhiên, ảnh Figure 3 hiện chỉ khoảng `210 × 139 px`, chữ nhỏ và phần legend khó đọc/có dấu hiệu bị cắt.

Yêu cầu khi tạo lại:

- Không chụp screenshot từ Word/Excel.
- Ưu tiên SVG/EMF; nếu dùng PNG, xuất tối thiểu khoảng `1200 px` chiều rộng hoặc 300 DPI ở kích thước in.
- Nền trắng, font và legend đủ lớn.
- Hiển thị rõ ba series: FCSR, FTPR, RSR.
- Không để chữ/legend chạm mép hoặc bị crop.
- Các nhãn phải thống nhất với nội dung bài:
  - No Repair: `RA=—; RT=—`
  - Fixed Repair: `RA=4 [3–6]; RT=350 s [325–375]`
  - Adaptive Repair: `RA=2 [1–4]; RT=200 s [180–220]`

Nếu hình quá chật, chỉ ghi median trong hình:

- Fixed Repair: `RA=4; RT=350 s`
- Adaptive Repair: `RA=2; RT=200 s`

IQR vẫn được trình bày trong phần văn bản.

### Caption tiếng Anh để đưa vào bài

> Figure 3. Effectiveness and efficiency of the evaluated repair mechanisms. The bars report FCSR, FTPR, and RSR, while RA and RT denote the median repair attempts and median wall-clock repair time, respectively.

### Bản dịch tiếng Việt để đọc hiểu

> Hình 3. Hiệu quả và hiệu suất của các cơ chế sửa lỗi được đánh giá. Các cột biểu diễn FCSR, FTPR và RSR; RA và RT lần lượt là số lần sửa lỗi trung vị và thời gian sửa lỗi thực tế trung vị.

## 1.4. Kiểm tra đoạn Assertion Prompt

Giữ đoạn hiện có bắt đầu bằng:

`Based on this post-hoc analysis, a strengthened assertion-oriented prompt specification is recommended...`

Đoạn này trả lời reviewer theo hướng không chạy lại: phân tích nguyên nhân, đề xuất prompt chặt hơn và nói rõ chưa có empirical improvement.

Định dạng `assertNotNull` bằng monospace/code nếu template cho phép.

Không được đổi câu cuối thành tuyên bố prompt mới đã cải thiện Mutation Score nếu chưa có thí nghiệm mới.

## Tiêu chí hoàn thành Phase 1

- [ ] `Answer to RQ3` dùng bản được chỉnh ở trên.
- [ ] Đoạn `Overall... did not fully replace...` đã được xóa.
- [ ] Ô Prompt của EvoSuite là `Search-based baseline`.
- [ ] Figure 3 được thay bằng ảnh/vector chất lượng in, không crop legend.
- [ ] Figure 3 thể hiện hoặc giải thích rõ RA và RT.
- [ ] Caption Figure 3 dùng bản chuẩn.

---

# PHASE 2 — Giữ nguyên những nội dung đã đúng

## 2.1. Data leakage

Giữ nguyên nội dung repository-level separation và Table I:

- 200 evaluation repositories được cố định trước fine-tuning.
- Mọi pair thuộc evaluation repositories được loại khỏi fine-tuning.
- `R_train ∩ R_evaluation = ∅`.
- Không có evaluation tuple trùng với fine-tuning tuple.

Trước khi nộp, phải có bằng chứng nội bộ cho phép kiểm tra overlap, ví dụ hai danh sách repository hoặc output của script intersection. Không bắt buộc public source code trong bài, nhưng không được báo cáo `0` nếu không có phép kiểm tra thật.

## 2.2. Selection/survivorship bias

Giữ nguyên:

- Abstract nói `conditional on reaching the valid state`.
- RQ3 giải thích invalid generations bị loại.
- Limitations nói quality values không đại diện toàn bộ 1.200 attempts.

## 2.3. Component contributions/ablation

Giữ nguyên:

- RQ1 tắt repair để đo initial-generation effect.
- RQ2 dùng cùng 120 failed candidates để so sánh No Repair, Fixed Repair và Adaptive Repair.
- Answer to RQ2 và Limitations nói rõ chưa isolate rollback, error-signature tracking, checkpointing và strategy switching.

Reviewer cho phép “bổ sung hoặc ít nhất thảo luận”, nên không cần tự tạo số ablation. Không được nói các component đã được chứng minh độc lập.

## 2.4. Model description

Giữ cách dùng thống nhất:

- `GPT-4: strong proprietary baseline`.
- `Fine-tuned Qwen2.5-Coder-32B: open, self-hosted experimental configuration`.
- ARROW là model-agnostic workflow.

## 2.5. Human tests và semantic correctness

Giữ nguyên:

- Human tests có fault-detection và structural quality mạnh hơn.
- ARROW bổ trợ, không thay thế developer-written tests.
- Compile/pass/mutation không tự chứng minh semantic correctness.
- Chưa có manual semantic audit hoặc dedicated oracle analysis.

## 2.6. Repair time và deployment limitations

Giữ thống nhất toàn bài:

- Fixed Repair: `350 s [325–375]`.
- Adaptive Repair: `200 s [180–220]`.
- Time reduction: `42.86%`.
- Median Adaptive latency: khoảng `3.3 minutes`.
- Chưa xác lập khả năng phù hợp với latency-sensitive CI/CD.
- Chưa đánh giá flaky-test behavior và inference/API cost.

## Tiêu chí hoàn thành Phase 2

- [ ] Không xuất hiện lại `920`, `1,180` hoặc `1180`.
- [ ] Không có tuyên bố ARROW thay thế human-written tests.
- [ ] Không có tuyên bố semantic correctness đã được chứng minh.
- [ ] Không có tuyên bố component-level causal contribution khi chưa ablation.
- [ ] Không có câu phủ nhận rằng bài đã có EvoSuite test-smell baseline.

---

# PHASE 3 — Các bước tăng độ chắc chắn với reviewer

Các bước này không yêu cầu chạy lại toàn bộ bài. Chỉ thực hiện nếu còn thời gian và muốn phản hồi mạnh hơn.

## 3.1. Assertion prompt: mức mạnh nhất

Bản hiện tại chỉ đề xuất prompt mới sau post-hoc analysis và trung thực rằng chưa đánh giá. Cách này thường đủ cho một bản revision đã accepted.

Nếu muốn tuyên bố prompt mới thực sự cải thiện Mutation Score, phải làm một paired follow-up nhỏ:

1. Chọn trước một subset 30–50 focal classes.
2. Dùng cùng model, repository context và execution conditions.
3. So sánh original prompt với assertion-strengthened prompt.
4. Báo compile/pass, Mutation Score và số surviving mutants.
5. Chỉ claim improvement nếu có kết quả thật.

Không cần làm bước này nếu không muốn thêm kết quả thực nghiệm mới. Khi không làm, giữ cách viết `recommended for subsequent ARROW runs` và `no empirical improvement is claimed`.

## 3.2. Manual semantic audit

Reviewer dùng từ “nên xem xét”, nên việc thừa nhận limitation có thể chấp nhận. Nếu muốn tăng độ chắc chắn:

- Chọn ngẫu nhiên một subset valid tests trước khi xem oracle quality.
- Đánh giá assertion có kiểm tra đúng intended behavior không.
- Ghi rõ tiêu chí, số mẫu và ai đánh giá.
- Không được chọn chỉ các test đẹp hoặc pass mutation cao.

Nếu không làm, giữ nguyên câu `No manual semantic audit or dedicated oracle-quality analysis was performed in this study.`

## 3.3. Evidence cho repository split

Lưu cùng artifact nội bộ:

- Danh sách canonical repository identifiers của fine-tuning set.
- Danh sách 200 evaluation repositories.
- Output phép set intersection bằng 0.
- Nếu có: random-selection procedure/seed hoặc manifest được cố định trước experiment.

Không bắt buộc đưa toàn bộ artifact vào bài, nhưng nên giữ để trả lời nếu ban biên tập hỏi.

---

# PHASE 4 — Tạo bảng phản hồi reviewer

Nên gửi kèm một file ngắn `Response_to_Reviewers.docx` hoặc đặt nội dung trong email. Mỗi ý gồm:

1. Trích ngắn nhận xét reviewer.
2. Nói đã sửa gì.
3. Chỉ đúng section/table/figure.
4. Không tranh luận dài hoặc tuyên bố ngoài dữ liệu.

## Mẫu phản hồi về Mutation Score/assertion prompt

### Bản tiếng Anh

> We expanded the RQ3 discussion to explain why high coverage does not necessarily imply strong fault detection. In particular, generated tests may exercise many statements while relying on weak or insufficiently behavior-specific assertions. We also added a strengthened assertion-oriented prompt specification covering returned values, state changes, side effects, boundary conditions, and expected exceptions. Because this refinement was formulated after the reported experiment, we explicitly state that the current results reflect the original prompts and do not claim an unmeasured improvement in Mutation Score.

### Bản dịch tiếng Việt

> Chúng tôi đã mở rộng phần thảo luận RQ3 để giải thích vì sao coverage cao không đồng nghĩa với khả năng phát hiện lỗi mạnh. Cụ thể, generated tests có thể thực thi nhiều câu lệnh nhưng sử dụng assertion yếu hoặc chưa đủ đặc thù theo hành vi. Chúng tôi cũng bổ sung đặc tả prompt tập trung vào assertion đối với giá trị trả về, thay đổi trạng thái, side effect, điều kiện biên và exception mong đợi. Vì hiệu chỉnh này được xây dựng sau thí nghiệm, bài nói rõ kết quả hiện tại phản ánh prompt cũ và không tuyên bố Mutation Score đã tăng khi chưa đo.

## Mẫu phản hồi về EvoSuite

### Bản tiếng Anh

> We added EvoSuite as a search-based test-smell baseline and applied the same tsDetect procedure and Smell Density definition to its valid generated suites. Among 30 valid EvoSuite suites, tsDetect reported 1,453 smell instances, corresponding to a Smell Density of 48.43, and no suite was smell-free. We explicitly state that this comparison is conditional on the valid subset and does not establish universal end-to-end superiority over EvoSuite.

### Bản dịch tiếng Việt

> Chúng tôi đã bổ sung EvoSuite làm đường cơ sở Test Smell dựa trên tìm kiếm và áp dụng cùng quy trình tsDetect cũng như cùng định nghĩa Smell Density cho các suite hợp lệ của EvoSuite. Trong 30 suite hợp lệ, tsDetect phát hiện 1.453 smell instances, tương ứng Smell Density 48,43, và không có suite nào hoàn toàn không có smell. Bài nói rõ phép so sánh này chỉ áp dụng cho valid subset và không chứng minh ARROW luôn vượt trội EvoSuite theo end-to-end.

---

# PHASE 5 — Kiểm tra cuối trước khi nộp

## 5.1. Tìm kiếm toàn văn trong Word

Dùng `Command + F`:

- `920` → 0 lần.
- `1,180` và `1180` → 0 lần.
- `0.50` → 0 lần đối với EvoSuite.
- `17 (56.67%)` → 0 lần.
- `does not include an EvoSuite` → 0 lần.
- `direct EvoSuite test-smell baselines` → 0 lần.
- `open-source baseline` khi nói Qwen → 0 lần.
- `did not fully replace` → 0 lần.
- `48.43` → thống nhất giữa bảng và phần phân tích.
- `200 seconds [IQR 180–220]` → thống nhất ở Abstract/RQ2/Limitations.

## 5.2. Kiểm tra Figure/Table

- [ ] Figure 1–4 liên tục, không thiếu số.
- [ ] Figure 3 mới rõ nét và không crop.
- [ ] Mọi figure được nhắc đến trong văn bản.
- [ ] Table I–III liên tục.
- [ ] Hàng EvoSuite không bị xuống dòng khó đọc.
- [ ] Caption nằm cùng trang với figure/table tương ứng.

## 5.3. Kiểm tra comment và tracked changes

- `Review` → xóa toàn bộ comments nội bộ.
- Tắt `Track Changes`.
- Accept All Changes nếu còn redline.
- Kiểm tra document properties nếu cần ẩn tên người comment/editor.

## 5.4. Template và số trang

Email yêu cầu đúng template VNICT 2026. Kiểm tra bằng template chính thức:

- Font, cỡ chữ, line spacing.
- Margin, header/footer và số cột.
- Caption Table/Figure.
- Equation không mất ký hiệu.
- Reference không tràn.
- Tổng số trang và phí trang vượt quá 6 trang nếu có.

## 5.5. Xuất PDF và kiểm tra từng trang

1. Lưu một bản DOCX mới, không ghi đè bản dự phòng.
2. Export PDF bằng Microsoft Word.
3. Kiểm tra từng trang, đặc biệt Figure 3, Figure 4 và bảng Test Smell.
4. Đối chiếu số liệu giữa DOCX, PDF và artifact gốc.
5. Chỉ gửi khi PDF không có chữ/hình bị cắt và mọi số liệu trùng nhau.

---

# Điều kiện sẵn sàng nộp

Các điều kiện bắt buộc:

- [ ] Hoàn thành toàn bộ Phase 1.
- [ ] Không làm sai lại các nội dung trong Phase 2.
- [ ] Có response-to-reviewers theo Phase 4.
- [ ] Hoàn thành kiểm tra Word/PDF/template trong Phase 5.
- [ ] Tất cả số liệu có artifact hoặc log nội bộ hỗ trợ.

Phase 3 là phần tăng độ chắc chắn, không bắt buộc nếu bạn không thêm claim mới. Không cần chạy lại toàn bộ 1.200 lượt chỉ để sửa bản revision.
