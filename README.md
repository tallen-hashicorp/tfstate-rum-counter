# Local Terraform RUM Counter

A standalone Python script to estimate Terraform Resources Under Management (RUM) from local state files.

**One state file measures one state, not an entire estate.** For an estate estimate, collect a current state from every relevant workspace, backend, project and environment across all Terraform estates. A directory total covers only the files you supply.

## Run

Requires Python 3.9+. Download `rum_counter.py`; nothing to install. The counter runs offline with no credentials, Terraform CLI or third-party packages.

```sh
python3 rum_counter.py production.tfstate
python3 rum_counter.py ./states --recursive
python3 rum_counter.py ./states -r --by-type --exclude 'archive' --exclude '*-old.tfstate'
mkdir -p reports
python3 rum_counter.py ./states -r --csv reports/rum.csv --json reports/rum.json
```

Example terminal output:

```text
State File         Estimated RUM  Data RS  Null RS  Total
-------------------------------------------------------
production.tfstate           169        7        0    176
-------------------------------------------------------
GRAND TOTAL                  169        7        0    176

Null RS includes null_resource and terraform_data. Totals exclude duplicate and skipped files.
Files: 1 counted, 0 duplicates ignored, 0 skipped; 0 scan errors; 0 paths excluded.
Total estimated RUM: 169 (supplied states only)
```

`--by-type` adds a breakdown of counted RUM by resource type, largest first, in the terminal and reports. Data sources, null resources and duplicate files do not contribute.

Repeat `--exclude PATTERN` to omit files or directories. Quote patterns so your shell does not expand them. Patterns match case-sensitive basenames or `/`-separated paths relative to the input directory; `*` can match across `/`. For a single file, matching uses its basename. A matching directory is pruned without reading its contents. The exclusion count is matched paths, not files inside excluded directories; default `.git` and `.terraform` omissions are not counted.

Directory scans match `*.tfstate`; recursion is opt-in. `.terraform`, `.git` and directory symlinks are not traversed. A directly supplied file can have any name. Raw state format version 4 is supported, including module resources, `count` and `for_each` instances. Older formats, plan files, `terraform show -json` output and malformed files are skipped with a reason.

Terminal output shows an aligned table with estimated RUM, data sources, null resources and total instances, plus grand totals. “Null RS” includes both `null_resource` and `terraform_data`. Duplicate files show their counts but are excluded from grand totals; skipped files show dashes. Warnings appear below the table. A summary shows counted, duplicate and skipped files, scan errors and excluded paths. Unreadable or unsupported input marks the estimate as partial. This summary also appears in reports; CSV uses `summary` rows, and optional type totals use `resource_type` rows.

Reports include skipped files and warnings. Exit status is `0` on completion, `1` for skipped/unreadable input, empty scans or report failures, and `2` for invalid arguments. Duplicate and review warnings do not change the exit status. Report destinations must be new files in existing directories.

## Counting rules

Checked against official HashiCorp documentation on **18 November 2026**:

- Count each instance whose resource has `mode = "managed"`, including modules.
- Exclude data sources, `null_resource` and `terraform_data`.
- Other local-only managed resources still count. Locals and variables are not resource instances.

Sources: [HCP pricing definitions](https://developer.hashicorp.com/hcp/docs/hcp/admin/billing/pricing-definitions) and [HCP Terraform managed resources](https://developer.hashicorp.com/terraform/cloud-docs/overview#managed-resources).

This is a point-in-time inventory estimate, not a billing calculator. State alone cannot establish first plan/apply timing, hourly peaks, resources absent from the snapshot, or contractual entitlements. Terraform Enterprise exposes its own [billable RUM usage](https://developer.hashicorp.com/terraform/enterprise/deploy/configuration/license); validate the estimate against the customer's agreement and platform usage.

Deposed objects retained during replacement are counted as stored instances and flagged. The cited billing definitions do not specify their treatment, so this count needs review.

## Duplicates

Identical parsed JSON states count once, even if whitespace or object-key order differs. The first file in sorted path order contributes to the total; duplicates retain their individual counts in reports.

Different snapshots sharing a lineage are flagged but **both remain in the total**. Supply only one current snapshot per workspace. Matching resource type and ID across different states also produces a warning, without subtracting resources: IDs may repeat across accounts or regions, and shared infrastructure does not establish a billing exemption. Missing IDs, changed IDs and other identity schemes limit detection. Review warnings before using the total.

## Export from Terraform OSS

For a local backend, copy the current `terraform.tfstate` (named workspaces normally use `terraform.tfstate.d/<workspace>/terraform.tfstate`). For a remote backend, run this from the initialized Terraform working directory with the intended workspace selected:

```sh
terraform workspace show
terraform state pull > /secure/export/production.tfstate
```

Use a new export filename. Repeat for every workspace and backend needed for the estimate; exclude backups and historical snapshots. [State pull](https://developer.hashicorp.com/terraform/cli/commands/state/pull) requires Terraform and backend access **during export only**, and upgrades the exported copy to a format supported by the installed CLI.

State files contain secrets. Keep exports secure. The counter only reads them and outputs paths, counts and fixed diagnostic text; it never outputs resource attributes, IDs, addresses, outputs or lineage values. Reports contain file paths, so choose filenames that reveal no sensitive information. Resource IDs are hashed in memory for comparison and are not written to reports.

`local-states/`, state files and `reports/` are Git-ignored. Never force-add customer exports. Tests use synthetic data:

```sh
python3 -m unittest -v
```

Inspired by [Terraform GCS RUM Counter](https://github.com/kevinmya/terraform-gcs-rum-counter). This implementation reads local files only and includes the documented `terraform_data` exclusion. Not an official HashiCorp billing tool.
