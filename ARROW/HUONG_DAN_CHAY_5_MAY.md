# Chạy nhanh chọn 200 sample trên 5 máy

Mỗi máy cần có sẵn Git, Python 3, Maven và Internet. Không cần API key.

## Chia máy

| Người chạy | Hệ điều hành | Shard index |
|---|---|---:|
| Dũng | Mac | 0 |
| Kiều Anh | Windows | 1 |
| Quang | Windows | 2 |
| Chính | Windows | 3 |
| Hán | Windows | 4 |

Cả năm máy chạy cùng lúc. Không cần đợi nhau.

Mỗi máy đang chạy hai candidate song song vì lệnh dùng `--workers 2`. Máy có
ít RAM có thể đổi thành `--workers 1`; máy mạnh có thể thử `--workers 3`, nhưng
nên giữ ở 2 để hạn chế Maven/Gradle tranh RAM, CPU và cache dependency.

## Bốn máy Windows

Trên mỗi máy, mở **PowerShell** từ Start Menu rồi mới chạy các lệnh bên dưới.
Không dùng Command Prompt (`cmd`) hoặc Git Bash. Nếu dùng terminal trong VS Code,
chọn terminal profile **PowerShell**.

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
```

Script cài đặt tự kích hoạt bốn JDK trong cửa sổ PowerShell hiện tại, nên có thể
chạy chọn sample ngay sau khi cài.

Mỗi người đặt `$ShardIndex` theo bảng trên rồi chạy. Ví dụ Kiều Anh dùng index 1:

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

Chỉ khi mở PowerShell mới, chạy hai lệnh sau trước khi chạy sample:

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
