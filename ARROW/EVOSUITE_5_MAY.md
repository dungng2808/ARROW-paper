# Chạy EvoSuite → JaCoCo/PIT → tsDetect trên 5 máy

Pipeline dùng chung:

```text
run_evosuite_full.py
```

Mỗi worker xử lý trọn một sample theo thứ tự:

```text
clone repo → EvoSuite → JaCoCo → PIT → tsDetect → lưu artifact → xóa repo cache
```

Không dùng `--keep-repo-cache`. Repo tạm trong `ARROW/repos/<project_id>` luôn
được xóa sau khi sample hoàn tất hoặc bị lỗi. Generated tests và raw report vẫn
được giữ trong `ARROW/runs/` để audit/resume.

## Phân công cố định

| Người | Hệ điều hành | `--shard-index` | Số sample |
|---|---|---:|---:|
| Dũng | macOS | 0 | 40 |
| Kiều Anh | Windows | 1 | 40 |
| Quang | Windows | 2 | 40 |
| Chính | Windows | 3 | 40 |
| Hán | Windows | 4 | 40 |

Cả năm máy dùng cùng file:

```text
shards/clean-samples-seed42/final/final_manifest_200.csv
```

Runner chia theo vị trí dòng trong manifest, không dùng candidate rank cũ, nên
mỗi máy nhận đúng 40 sample.

## Chuẩn bị chung

Chỉ bắt đầu sau khi code này đã được commit/push lên `main`. Mỗi máy pull rồi
đứng trong folder `ARROW`.

### macOS — Dũng

```bash
chmod +x scripts/install-java-versions-macos.sh
./scripts/install-java-versions-macos.sh
source Java-version/activate-java-versions.sh

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

python run_evosuite_full.py --download-tools --setup-only
```

### Windows — bốn máy còn lại

Mở **PowerShell** trong folder `ARROW`:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install-java-versions.ps1
. .\Java-version\activate-java-versions.ps1

py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

.\.venv\Scripts\python.exe run_evosuite_full.py --download-tools --setup-only
```

Setup tự tải và kiểm SHA256 của EvoSuite 1.2.0, JaCoCo 0.8.12, PIT 1.17.4 và
tsDetect 2.2. Các JAR nằm trong `ARROW/tools/`, đã bị Git ignore.

## Dũng — macOS — shard 0

Smoke một sample:

```bash
python run_evosuite_full.py \
  --manifest shards/clean-samples-seed42/final/final_manifest_200.csv \
  --run-id evosuite-clean-200-part-0 \
  --output-dir runs/evosuite/evosuite-clean-200-part-0 \
  --shard-count 5 --shard-index 0 \
  --limit 1 --workers 1 --pit-threads 1 \
  --seeds 42 --search-budget 120 --download-tools
```

Nếu smoke xong, chạy full bằng đúng run ID để resume sample đầu:

```bash
python run_evosuite_full.py \
  --manifest shards/clean-samples-seed42/final/final_manifest_200.csv \
  --run-id evosuite-clean-200-part-0 \
  --output-dir runs/evosuite/evosuite-clean-200-part-0 \
  --compact-dir shard-results/evosuite/shard-0 \
  --shard-count 5 --shard-index 0 \
  --workers 2 --pit-threads 1 \
  --seeds 42 --search-budget 120 --download-tools
```

## Windows — mẫu lệnh cho shard 1–4

Thay toàn bộ `I` bằng shard index được giao, gồm cả tên run/folder. Ví dụ Kiều
Anh dùng `I=1`:

```powershell
.\.venv\Scripts\python.exe run_evosuite_full.py `
  --manifest shards\clean-samples-seed42\final\final_manifest_200.csv `
  --run-id evosuite-clean-200-part-1 `
  --output-dir runs\evosuite\evosuite-clean-200-part-1 `
  --shard-count 5 --shard-index 1 `
  --limit 1 --workers 1 --pit-threads 1 `
  --seeds 42 --search-budget 120 --download-tools
```

Sau khi smoke đạt, bỏ `--limit 1`, tăng `--workers 2` và thêm compact folder:

```powershell
.\.venv\Scripts\python.exe run_evosuite_full.py `
  --manifest shards\clean-samples-seed42\final\final_manifest_200.csv `
  --run-id evosuite-clean-200-part-1 `
  --output-dir runs\evosuite\evosuite-clean-200-part-1 `
  --compact-dir shard-results\evosuite\shard-1 `
  --shard-count 5 --shard-index 1 `
  --workers 2 --pit-threads 1 `
  --seeds 42 --search-budget 120 --download-tools
```

Áp dụng tương tự:

```text
Quang:    part-2, shard-2, --shard-index 2
Chính:    part-3, shard-3, --shard-index 3
Hán:      part-4, shard-4, --shard-index 4
```

## Kiểm tra trước khi gửi kết quả

Mỗi máy mở:

```text
runs/evosuite/evosuite-clean-200-part-I/completeness_report.json
```

Phải có:

```text
expected_records = 40
generation_records_n = 40
duplicate_key_n = 0
missing_generation_record_n = 0
table_iii_ready = true
full_quality_ready = true
repo_cache_removed_n = 40
```

Nếu `full_quality_ready=false`, đọc `*_error`, raw log trong sample artifact và
chạy lại đúng lệnh full nhưng thêm:

```text
--rerun-status PARTIAL --rerun-status INFRA_ERROR
```

`GENERATION_INVALID` là kết quả EvoSuite không sinh được suite hợp lệ, không tự
retry và không đổi sample. Không xóa record lỗi hoặc ghi metric thiếu thành 0.

## Gửi compact result qua Git

Chỉ gửi folder nhỏ sau khi shard đạt gate; không gửi `runs`, `repos`, `tools`,
`.venv` hoặc `Java-version`.

Mỗi người chỉ stage folder của mình:

```text
ARROW/shard-results/evosuite/shard-I/
```

Commit message:

```text
data: add EvoSuite full metrics shard I (Tên)
```

Sau đó pull rebase và push `main`; không force-push, không dùng `git add .`.

## Dũng merge sau khi đủ năm shard

Từ folder `ARROW`:

```bash
python run_evosuite_full.py \
  --manifest shards/clean-samples-seed42/final/final_manifest_200.csv \
  --run-id evosuite-clean-200-merged \
  --output-dir runs/evosuite/evosuite-clean-200-merged \
  --shard-count 5 \
  --merge-run-dir shard-results/evosuite/shard-0 \
  --merge-run-dir shard-results/evosuite/shard-1 \
  --merge-run-dir shard-results/evosuite/shard-2 \
  --merge-run-dir shard-results/evosuite/shard-3 \
  --merge-run-dir shard-results/evosuite/shard-4
```

Merge từ chối thiếu/trùng shard, sai manifest SHA, sai seed, sai phân vùng hoặc
thiếu generation key. Kết quả cuối nằm tại:

```text
runs/evosuite/evosuite-clean-200-merged/
```

File chính:

```text
evosuite_full_quality_records.jsonl
evosuite_full_quality_summary.json
table_iii_evosuite.csv
completeness_report.json
```
