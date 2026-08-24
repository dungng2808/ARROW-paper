# Quy trình bắt buộc cho Agent chạy EvoSuite full metrics

File này dành cho coding Agent. Agent phải tự thực thi đến khi hoàn thành, không
chỉ giải thích hoặc đưa lệnh cho người dùng tự chạy.

## Biến do file vai trò cung cấp

```text
OPERATOR_NAME
SHARD_INDEX       # 0..4
EXPECTED_OS       # macos hoặc windows
COORDINATOR       # true chỉ với Dũng
```

Giá trị khóa chung:

```text
SHARD_COUNT=5
SAMPLES_PER_SHARD=40
SEED=42
SEARCH_BUDGET=120
WORKERS=2
PIT_THREADS=1
MANIFEST_SHA256=e067e8a8501e0396fe52d84627a6770ca1342792d8b8ead1723ce2c1454aa0d0
```

## Yêu cầu hành vi

1. Tự chạy toàn bộ setup, smoke, full run, kiểm lỗi, retry và publish compact
   result. Không yêu cầu người dùng copy lệnh.
2. Không reset, restore, stash, xóa hoặc ghi đè thay đổi local của người dùng.
3. Nếu worktree bẩn hoặc pull bị chặn, tạo clone sạch riêng để chạy. Không dùng
   `git clean`, `git reset --hard` hoặc force-push.
4. Raw output trong `runs`, repo cache, JDK, `.venv` và tool JAR không được đưa
   lên Git.
5. Không đổi manifest, seed, search budget, shard count/index hoặc sample.
6. Không dùng kết quả EvoSuite/metrics để thay sample.
7. Repo cache phải được xóa sau từng sample. Không thêm `--keep-workspace` trong
   full run và không có `--keep-repo-cache`.
8. Chỉ publish compact result khi shard có đủ 40 generation record, không thiếu
   hoặc trùng key và mọi metric của suite VALID đã hoàn tất.

## 1. Chuẩn bị repository

Tìm repository root thay vì đoán tên folder:

```text
git rev-parse --show-toplevel
git remote get-url origin
git status --short
```

Remote chuẩn:

```text
https://github.com/dungng2808/ARROW-paper.git
```

Trong clone sạch:

```text
git switch main
git pull --ff-only origin main
```

Xác nhận tồn tại:

```text
ARROW/run_evosuite_full.py
ARROW/src/evosuite_full.py
ARROW/src/evosuite_quality.py
ARROW/shards/clean-samples-seed42/final/final_manifest_200.csv
```

Nếu thiếu bất kỳ file nào, fetch/pull lại. Nếu `origin/main` vẫn thiếu, dừng và
báo rõ rằng code EvoSuite integrated chưa được push; không tự dùng ba script cũ.

Đứng trong folder `ARROW` ở các bước tiếp theo.

## 2. Kiểm tra manifest và đúng hệ điều hành

- Từ chối chạy nếu OS thực tế khác `EXPECTED_OS`.
- Manifest phải có đúng 200 data row và 200 `project_id` khác nhau.
- SHA256 phải đúng `MANIFEST_SHA256`.
- Dry-run của shard phải báo `selected_this_run=40` và `expected_records=40`.

## 3. Cài Java, Python và tool

### Windows — bắt buộc PowerShell

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install-java-versions.ps1
. .\Java-version\activate-java-versions.ps1

if (-not (Test-Path .venv)) { py -3 -m venv .venv }
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run_evosuite_full.py --download-tools --setup-only
```

### macOS

```bash
chmod +x scripts/install-java-versions-macos.sh
./scripts/install-java-versions-macos.sh
source Java-version/activate-java-versions.sh

test -d .venv || python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python run_evosuite_full.py --download-tools --setup-only
```

Xác nhận setup nhận diện JDK 8/11/17/21 và đúng các tool:

```text
EvoSuite 1.2.0
JaCoCo 0.8.12
PIT 1.17.4
tsDetect 2.2
```

## 4. Tên run/folder bắt buộc

```text
RUN_ID=evosuite-clean-200-part-${SHARD_INDEX}
OUTPUT_DIR=runs/evosuite/evosuite-clean-200-part-${SHARD_INDEX}
COMPACT_DIR=shard-results/evosuite/shard-${SHARD_INDEX}
```

## 5. Dry-run và smoke

Chạy dry-run đúng shard. Agent tự chuyển sang cú pháp PowerShell/Bash:

```text
python run_evosuite_full.py
  --manifest shards/clean-samples-seed42/final/final_manifest_200.csv
  --run-id evosuite-clean-200-part-${SHARD_INDEX}
  --output-dir runs/evosuite/evosuite-clean-200-part-${SHARD_INDEX}
  --shard-count 5
  --shard-index ${SHARD_INDEX}
  --workers 2
  --seeds 42
  --dry-run
```

Sau đó chạy smoke một sample:

```text
python run_evosuite_full.py
  --manifest shards/clean-samples-seed42/final/final_manifest_200.csv
  --run-id evosuite-clean-200-part-${SHARD_INDEX}
  --output-dir runs/evosuite/evosuite-clean-200-part-${SHARD_INDEX}
  --shard-count 5
  --shard-index ${SHARD_INDEX}
  --limit 1
  --workers 1
  --pit-threads 1
  --seeds 42
  --search-budget 120
  --download-tools
```

Smoke phải tạo generation record và xóa repo cache của sample. Nếu suite VALID,
phải có raw JaCoCo, PIT và tsDetect artifact. Sửa lỗi code/tool có tính tổng
quát nếu smoke lỗi hạ tầng; không đổi sample để né lỗi.

## 6. Chạy đủ 40 sample

Chạy cùng run ID/output để resume smoke:

```text
python run_evosuite_full.py
  --manifest shards/clean-samples-seed42/final/final_manifest_200.csv
  --run-id evosuite-clean-200-part-${SHARD_INDEX}
  --output-dir runs/evosuite/evosuite-clean-200-part-${SHARD_INDEX}
  --compact-dir shard-results/evosuite/shard-${SHARD_INDEX}
  --shard-count 5
  --shard-index ${SHARD_INDEX}
  --workers 2
  --pit-threads 1
  --seeds 42
  --search-budget 120
  --download-tools
```

Giữ tiến trình đến khi hoàn tất. Nếu máy/network dừng, chạy lại đúng lệnh để
resume. Không thêm `--no-resume`.

## 7. Gate và retry

Đọc:

```text
runs/evosuite/evosuite-clean-200-part-${SHARD_INDEX}/completeness_report.json
```

Các giá trị bắt buộc:

```text
expected_records == 40
generation_records_n == 40
duplicate_key_n == 0
missing_generation_record_n == 0
repo_cache_removed_n == 40
table_iii_ready == true
full_quality_ready == true
coverage_complete_n == valid_test_n
mutation_complete_or_not_applicable_n == valid_test_n
smell_complete_n == valid_test_n
full_metric_record_n == valid_test_n
```

Nếu có `PARTIAL` hoặc `INFRA_ERROR`, đọc raw log, sửa lỗi hạ tầng/code tổng quát
rồi chạy lại full command với:

```text
--rerun-status PARTIAL --rerun-status INFRA_ERROR
```

`GENERATION_INVALID` là kết quả EvoSuite không tạo được suite hợp lệ. Giữ record
đó trong mẫu số, không tự retry vô hạn và không đổi sample. Metric của record này
phải là N/A, không phải 0.

## 8. Publish compact result

Xác nhận compact folder có đúng các file:

```text
provenance.json
evosuite_records.jsonl
quality_records.jsonl
smell_records.jsonl
evosuite_full_quality_records.jsonl
evosuite_full_quality_summary.json
table_iii_evosuite.csv
completeness_report.json
```

Từ repository root, chỉ stage:

```text
ARROW/shard-results/evosuite/shard-${SHARD_INDEX}/
```

Không dùng `git add .`. Commit message:

```text
data: add EvoSuite metrics shard ${SHARD_INDEX} (${OPERATOR_NAME})
```

Sau đó:

```text
git pull --rebase origin main
git push origin HEAD:main
```

Nếu push bị từ chối vì máy khác vừa push, pull rebase và push lại. Không
force-push. Xác nhận compact folder của mình tồn tại trên `origin/main`.

## 9. Coordinator Dũng merge

Nếu `COORDINATOR=false`, dừng sau khi publish shard của mình.

Nếu `COORDINATOR=true`, Agent phải tiếp tục theo dõi `origin/main` đến khi đủ:

```text
ARROW/shard-results/evosuite/shard-0/
...
ARROW/shard-results/evosuite/shard-4/
```

Khi đủ năm shard, từ folder `ARROW` chạy:

```text
python run_evosuite_full.py
  --manifest shards/clean-samples-seed42/final/final_manifest_200.csv
  --run-id evosuite-clean-200-merged
  --output-dir runs/evosuite/evosuite-clean-200-merged
  --shard-count 5
  --merge-run-dir shard-results/evosuite/shard-0
  --merge-run-dir shard-results/evosuite/shard-1
  --merge-run-dir shard-results/evosuite/shard-2
  --merge-run-dir shard-results/evosuite/shard-3
  --merge-run-dir shard-results/evosuite/shard-4
```

Merge phải có `expected_records=200`, không thiếu/trùng key,
`table_iii_ready=true` và `full_quality_ready=true`. Báo lại hàng
`table_iii_evosuite.csv`, IC/LC/BC/MC/MS, tổng 21 smell entity, đường dẫn output,
commit của năm shard và xác nhận repo cache đã được xóa.

