# Chọn 200 sample baseline-clean và chia tải cho 5 máy

`select_clean_samples.py` chọn đúng một sample/repository bằng seed cố định,
lọc focal method trivial trước khi clone, kiểm tra semantic của focal source,
sau đó mới chạy baseline/classpath/probe/offline build. Script không gọi LLM.

## Tiêu chí semantic mặc định

Sample bị loại trước experiment nếu thuộc một trong các trường hợp:

- focal source không nằm dưới `src/main/java`;
- focal declaration là interface, annotation, enum, record hoặc abstract class;
- focal method rỗng, private, native hoặc abstract;
- focal method chỉ là getter/setter, trả field/constant hoặc delegate một lời gọi;
- không có output, exception hoặc state change quan sát được;
- source có dấu hiệu cần database, network, Spring/Android context, filesystem,
  process/native API hoặc nguồn nondeterminism;
- testability score nhỏ hơn 7/10.

Các tiêu chí này chỉ dùng dữ liệu/source trước generation. Coverage, mutation,
model output và generated-test status không tham gia lựa chọn.

## Chạy trên một máy

```bash
cd ARROW

./.venv/bin/python select_clean_samples.py \
  --run-id clean-200-seed42 \
  --target 200 \
  --reserve 50 \
  --candidate-count 3000 \
  --seed 42 \
  --workers 2 \
  --batch-size 25 \
  --baseline-repeats 2 \
  --exclude-manifest shards/repo_shard_05_manifest.csv
```

Chạy lại đúng lệnh và `run-id` để resume. Không dùng `--no-resume` trừ khi
muốn bỏ audit cũ và chạy lại từ đầu.

## Chạy trên 5 máy

### Bước 1: khóa candidate trên máy điều phối

Nên tạo pool 3000 repository vì semantic filter chặt hơn baseline filter cũ.

```bash
./.venv/bin/python select_clean_samples.py \
  --run-id clean-200-candidates-seed42 \
  --output-dir shards/clean-samples-seed42 \
  --export-dataset-dir shards/clean-samples-seed42/dataset \
  --target 200 \
  --reserve 50 \
  --candidate-count 3000 \
  --seed 42 \
  --exclude-manifest shards/repo_shard_05_manifest.csv \
  --prepare-only
```

Lệnh trên export đúng 3000 JSON candidate thay vì toàn bộ dataset 5.4 GB. Pool
3000 hiện khoảng 29 MB và được lưu theo layout `<project>/<sample>.json`. Commit
candidate, provenance và mini dataset để 5 máy tự lấy qua Git:

```bash
git add shards/clean-samples-seed42
git commit -m "data: lock clean sample candidates seed 42"
git push
```

Không mở/lưu lại CSV bằng Excel vì có thể đổi newline hoặc encoding. Đồng thời
bảo đảm 5 máy dùng cùng commit source, `pipeline.yaml`, dataset và bộ JDK
8/11/17/21. SHA256 candidate được kiểm tra lúc merge.

### Bước 2: chạy từng shard

Trên máy số `i`, thay `SHARD_INDEX` bằng một giá trị từ 0 đến 4:

```bash
git fetch origin
git pull --ff-only

SHARD_INDEX=0

./.venv/bin/python select_clean_samples.py \
  --dataset shards/clean-samples-seed42/dataset \
  --candidate-manifest shards/clean-samples-seed42/candidate_manifest.csv \
  --run-id clean-200-shard-${SHARD_INDEX} \
  --output-dir runs/sample_selection/distributed/shard-${SHARD_INDEX} \
  --target 200 \
  --reserve 50 \
  --seed 42 \
  --shard-count 5 \
  --shard-index ${SHARD_INDEX} \
  --workers 2 \
  --batch-size 25 \
  --baseline-repeats 2
```

Phân vùng dùng công thức `(candidate_rank - 1) mod 5`, vì vậy không có hai máy
chạy cùng repository. Trong distributed mode, mỗi máy xử lý toàn bộ candidate
được giao để merge vẫn chọn đúng 200 eligible đầu tiên theo rank toàn cục.

Nếu một máy bị dừng, chạy lại đúng lệnh, `run-id`, `output-dir` và shard index.
Audit được checkpoint sau mỗi batch.

### Bước 3: gom và merge

Copy nguyên 5 thư mục output về máy điều phối, ví dụ:

```text
runs/sample_selection/distributed/shard-0
runs/sample_selection/distributed/shard-1
runs/sample_selection/distributed/shard-2
runs/sample_selection/distributed/shard-3
runs/sample_selection/distributed/shard-4
```

Merge:

```bash
./.venv/bin/python select_clean_samples.py \
  --candidate-manifest shards/clean-samples-seed42/candidate_manifest.csv \
  --run-id clean-200-merged-seed42 \
  --output-dir runs/sample_selection/clean-200-merged-seed42 \
  --target 200 \
  --reserve 50 \
  --merge-shard-dir runs/sample_selection/distributed/shard-0 \
  --merge-shard-dir runs/sample_selection/distributed/shard-1 \
  --merge-shard-dir runs/sample_selection/distributed/shard-2 \
  --merge-shard-dir runs/sample_selection/distributed/shard-3 \
  --merge-shard-dir runs/sample_selection/distributed/shard-4
```

Merge từ chối kết quả nếu thiếu shard, trùng shard index, candidate nằm sai
phân vùng hoặc SHA256 candidate không khớp.

## Output chính

```text
final_manifest_200.csv    # dùng cho RQ1/EvoSuite
reserve_manifest_50.csv   # chỉ thay thế lỗi hạ tầng theo rule đã khóa
eligible_manifest.csv
preflight_audit.csv
selection_summary.json
provenance.json
final_manifest_200_shard_0_of_5.csv  # phần chạy tiếp trên máy 0
...
final_manifest_200_shard_4_of_5.csv  # phần chạy tiếp trên máy 4
```

Các manifest chia theo shard sau merge giữ sample trên đúng máy đã qualification.
Nếu repo cache vẫn còn trên từng máy, dùng file tương ứng để chạy RQ1/EvoSuite
và tránh clone lại. Nếu muốn chạy toàn bộ trên máy điều phối, phải copy cả repo
cache hoặc chấp nhận clone lại.

Ví dụ sau khi copy manifest phần 0 trở lại máy 0:

```bash
./.venv/bin/python run_RQ1.py \
  --manifest final_manifest_200_shard_0_of_5.csv \
  --agent YOUR_AGENT \
  --run-id rq1-clean-200-part-0 \
  --workers 2 \
  --keep-repo-cache
```

Mỗi máy dùng run-id riêng. Sau cùng merge report RQ1 theo cơ chế report của
pipeline; không nối CSV bằng tay.

Nếu `selection_summary.json` báo chưa đủ 200 + 50, tạo một run candidate mới
với pool lớn hơn. Không bổ sung sample dựa trên kết quả generation.

## Nới filter khi thật sự cần

Các cờ sau làm giảm độ đảm bảo và phải được ghi trong methodology:

```text
--include-non-concrete
--allow-external-risk
--allow-nonstandard-source-layout
--min-testability-score 6
--skip-classpath-check
--skip-probe-test
--skip-offline-check
```

Không nên dùng các cờ này cho manifest chính nếu mục tiêu là chỉ còn failure do
generated test.
