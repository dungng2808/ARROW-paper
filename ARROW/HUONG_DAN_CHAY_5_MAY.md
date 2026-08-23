# Chạy nhanh chọn 200 sample trên 5 máy

Mỗi máy cần có sẵn Git, Python 3, Maven và Internet. Không cần API key.

## Chia máy

| Máy | Hệ điều hành | Shard index |
|---|---|---:|
| Máy chính | Mac | 0 |
| Máy 2 | Windows | 1 |
| Máy 3 | Windows | 2 |
| Máy 4 | Windows | 3 |
| Máy 5 | Windows | 4 |

Cả năm máy chạy cùng lúc. Không cần đợi nhau.

## Bốn máy Windows

Nếu chưa có code:

```powershell
git clone https://github.com/dungng2808/ARROW-paper.git
```

Chuẩn bị và tải Java 8/11/17/21 vào `ARROW/Java-version`:

```powershell
Set-Location ARROW-paper
git switch main
git pull --ff-only origin main
Set-Location ARROW

py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install-java-versions.ps1
. .\Java-version\activate-java-versions.ps1
```

Mỗi máy đặt `$ShardIndex` theo bảng trên rồi chạy. Ví dụ máy 2 dùng index 1:

```powershell
$ShardIndex = 1

.\.venv\Scripts\python.exe select_clean_samples.py `
  --dataset shards/clean-samples-seed42/dataset `
  --candidate-manifest shards/clean-samples-seed42/candidate_manifest.csv `
  --run-id "clean-200-shard-$ShardIndex" `
  --output-dir "runs/sample_selection/distributed/shard-$ShardIndex" `
  --target 200 `
  --reserve 50 `
  --seed 42 `
  --shard-count 5 `
  --shard-index $ShardIndex `
  --workers 2 `
  --batch-size 25 `
  --baseline-repeats 2
```

Các máy Windows còn lại chỉ thay `$ShardIndex` thành 2, 3 hoặc 4.

Nếu mở PowerShell mới trước khi chạy lại:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
. .\Java-version\activate-java-versions.ps1
```

Nếu bị dừng, chạy lại đúng lệnh cũ để script tự tiếp tục.

## Máy Mac

```bash
cd ARROW-paper
git switch main
git pull --ff-only origin main
cd ARROW

python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt

./scripts/install-java-versions-macos.sh
source Java-version/activate-java-versions.sh

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

Nếu bị dừng, chạy lại đúng lệnh trên để tiếp tục.

## Gửi kết quả về máy Mac

Mỗi máy Windows chỉ cần gửi hai file sau, giữ đúng shard index của máy:

```text
runs/sample_selection/distributed/shard-I/preflight_audit.csv
runs/sample_selection/distributed/shard-I/provenance.json
```

Trên máy Mac, đặt kết quả vào:

```text
ARROW/shard-results/shard-0/
ARROW/shard-results/shard-1/
ARROW/shard-results/shard-2/
ARROW/shard-results/shard-3/
ARROW/shard-results/shard-4/
```

Mỗi thư mục phải có `preflight_audit.csv` và `provenance.json`.

## Merge lấy 200 sample cuối

Trên máy Mac:

```bash
cd ARROW-paper/ARROW

./.venv/bin/python select_clean_samples.py \
  --candidate-manifest shards/clean-samples-seed42/candidate_manifest.csv \
  --run-id clean-200-merged-seed42 \
  --output-dir runs/sample_selection/clean-200-merged-seed42 \
  --target 200 \
  --reserve 50 \
  --merge-shard-dir shard-results/shard-0 \
  --merge-shard-dir shard-results/shard-1 \
  --merge-shard-dir shard-results/shard-2 \
  --merge-shard-dir shard-results/shard-3 \
  --merge-shard-dir shard-results/shard-4
```

File sample cuối cùng:

```text
runs/sample_selection/clean-200-merged-seed42/final_manifest_200.csv
```

`Java-version`, `.venv`, `runs` và `repos` đều là dữ liệu local đã được Git
ignore; không push các thư mục này lên Git.
