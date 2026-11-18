#!/usr/bin/env python3
"""Offline, point-in-time RUM estimates from raw Terraform state v4."""

import argparse
import csv
import hashlib
from fnmatch import fnmatchcase
import json
import os
from pathlib import Path
import sys

EXCLUDED_TYPES = frozenset({'null_resource', 'terraform_data'})
RULES_CHECKED = '2026-11-18'
FIELDS = ['file', 'status', 'rum', 'included_rum', 'data_instances',
          'excluded_instances', 'deposed_instances', 'reason', 'warnings']


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=True).encode()).digest()


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def reject_constant(_):
    raise ValueError('non-JSON number')


def inspect_state(path):
    """Return public counts and private comparison keys; never return attributes."""
    state = json.loads(path.read_bytes(), object_pairs_hook=strict_object,
                       parse_constant=reject_constant)
    if not isinstance(state, dict) or type(state.get('version')) is not int or state['version'] != 4:
        raise ValueError('unsupported state format (requires raw state version 4)')
    resources = state.get('resources')
    if not isinstance(resources, list):
        raise ValueError('invalid or missing resources array')
    counts = dict(rum=0, data_instances=0, excluded_instances=0, deposed_instances=0)
    identities = set()
    by_type = {}
    missing_ids = 0
    for resource in resources:
        if not isinstance(resource, dict):
            raise ValueError('invalid resource entry')
        mode, kind = resource.get('mode'), resource.get('type')
        instances = resource.get('instances')
        if mode not in ('managed', 'data') or not isinstance(kind, str) or not kind or not isinstance(instances, list):
            raise ValueError('unsupported resource mode or invalid resource structure')
        for instance in instances:
            if not isinstance(instance, dict) or not isinstance(instance.get('attributes'), dict):
                raise ValueError('invalid instance or attributes structure')
            if mode == 'data':
                counts['data_instances'] += 1
            elif kind in EXCLUDED_TYPES:
                counts['excluded_instances'] += 1
            else:
                counts['rum'] += 1
                by_type[kind] = by_type.get(kind, 0) + 1
                counts['deposed_instances'] += bool(instance.get('deposed'))
                identifier = instance['attributes'].get('id')
                if isinstance(identifier, (str, int)) and not isinstance(identifier, bool) and identifier != '':
                    # Type + ID is only a heuristic: IDs may be scoped to an account.
                    identities.add(fingerprint([kind, identifier]))
                else:
                    missing_ids += 1
    warnings = []
    if counts['deposed_instances']:
        warnings.append('Includes deposed replacement objects; billing treatment needs review')
    if missing_ids:
        warnings.append(f'{missing_ids} counted instances lack a usable ID for overlap detection')
    lineage = state.get('lineage')
    return counts, warnings, fingerprint(state), identities, (
        fingerprint(lineage) if isinstance(lineage, str) and lineage else None), by_type


def scan(paths, by_type=False):
    type_totals = {}
    rows, seen, identity_owners, lineage_owners = [], {}, {}, {}
    for path in paths:
        row = dict(file=str(path), status='skipped', rum=None, included_rum=0,
                   data_instances=None, excluded_instances=None, deposed_instances=None,
                   reason='', warnings=[])
        rows.append(row)
        try:
            counts, warnings, digest, identities, lineage, types = inspect_state(path)
        except (OSError, UnicodeError):
            row['reason'] = 'Cannot read file as JSON text'
            continue
        except (ValueError, RecursionError, OverflowError):
            # Never echo parser errors: they may contain customer state values.
            row['reason'] = 'Malformed or unsupported state; requires raw Terraform state version 4 with valid resources and instances'
            continue
        row.update(counts)
        row['warnings'] = warnings
        if digest in seen:
            row.update(status='duplicate', reason='Identical JSON state; already counted: ' + seen[digest])
            continue
        seen[digest] = str(path)
        row.update(status='counted', included_rum=counts['rum'])
        if by_type:
            for kind, count in types.items():
                type_totals[kind] = type_totals.get(kind, 0) + count
        if lineage is not None:
            for other in lineage_owners.get(lineage, []):
                row['warnings'].append('Shared lineage with ' + other['file'] + '; possible historical snapshot, both included')
                other['warnings'].append('Shared lineage with ' + str(path) + '; possible historical snapshot, both included')
            lineage_owners.setdefault(lineage, []).append(row)
        overlaps = {}
        for identity in identities:
            for index in identity_owners.get(identity, []):
                overlaps[index] = overlaps.get(index, 0) + 1
            identity_owners.setdefault(identity, []).append(len(rows) - 1)
        for index, count in sorted(overlaps.items()):
            other = rows[index]
            row['warnings'].append(f'{count} potential shared resource IDs with {other["file"]}; both included')
            other['warnings'].append(f'{count} potential shared resource IDs with {path}; both included')
    report = dict(rules_checked=RULES_CHECKED, files=rows,
                  total_rum=sum(row['included_rum'] for row in rows))
    if by_type:
        report['by_type'] = dict(sorted(type_totals.items(), key=lambda item: (-item[1], item[0])))
    return report


def discover(source, recursive, exclude=()):
    excluded = []
    base = source.parent if source.is_file() else source

    def is_excluded(path, directory=False):
        relative = path.relative_to(base).as_posix()
        if any(fnmatchcase(relative, pattern) or fnmatchcase(path.name, pattern)
               for pattern in exclude):
            excluded.append(str(path) + ('/' if directory else ''))
            return True
        return False

    if source.is_file():
        return ([] if is_excluded(source) else [source]), [], excluded
    if not source.is_dir():
        return [], ['Input is not a readable file or directory'], excluded
    paths, errors = [], []
    def onerror(error):
        errors.append('Cannot scan directory: ' + str(error.filename))
    for root, dirs, files in os.walk(source, onerror=onerror, followlinks=False):
        dirs[:] = [d for d in sorted(dirs) if d not in ('.git', '.terraform')
                   and not is_excluded(Path(root) / d, directory=True)] if recursive else []
        paths.extend(Path(root) / name for name in sorted(files)
                     if name.endswith('.tfstate') and not is_excluded(Path(root) / name))
    return sorted(paths), errors, excluded


def safe_cell(value):
    """Avoid spreadsheet formula execution from user-controlled file paths."""
    text = str(value)
    return "'" + text if text.lstrip().startswith(('=', '+', '-', '@')) or text.startswith(('\t', '\r', '\n')) else text


def write_report(path, report, as_csv=False):
    # Exclusive creation also prevents overwriting state via symlinks/hardlinks.
    with path.open('x', encoding='utf-8', newline='') as stream:
        if as_csv:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            for row in report['files']:
                values = dict(row, warnings='; '.join(row['warnings']))
                writer.writerow({k: safe_cell(v) if v is not None else '' for k, v in values.items()})
            writer.writerow(dict(file='TOTAL', status='total', included_rum=report['total_rum']))
            for metric, count in report['summary'].items():
                writer.writerow(dict(file=metric, status='summary', reason=count))
            for kind, count in report.get('by_type', {}).items():
                writer.writerow(dict(file=safe_cell(kind), status='resource_type', included_rum=count))
            for excluded in report['excluded_paths']:
                writer.writerow(dict(file=safe_cell(excluded), status='excluded'))
            for error in report['scan_errors']:
                writer.writerow(dict(status='skipped', reason=safe_cell(error)))
        else:
            json.dump(report, stream, indent=2, ensure_ascii=True)
            stream.write('\n')


def print_table(report):
    headers = ['State File', 'Estimated RUM', 'Data RS', 'Null RS', 'Total']
    rows = []
    totals = [0, 0, 0, 0]
    for row in report['files']:
        # Escape control characters in paths so they cannot alter terminal output.
        label = json.dumps(row['file'], ensure_ascii=True)[1:-1]
        if row['status'] != 'counted':
            label += ' [' + row['status'].upper() + ']'
        if row['rum'] is None:
            values = ['-'] * 4
        else:
            values = [row['rum'], row['data_instances'], row['excluded_instances']]
            values.append(sum(values))
            if row['status'] == 'counted':
                totals = [a + b for a, b in zip(totals, values)]
        rows.append([label] + [str(value) for value in values])
    footer = ['GRAND TOTAL'] + [str(value) for value in totals]
    widths = [max(len(row[i]) for row in [headers, *rows, footer]) for i in range(5)]

    def line(row):
        return '  '.join(value.ljust(widths[i]) if i == 0 else value.rjust(widths[i])
                         for i, value in enumerate(row))

    separator = '-' * (sum(widths) + 8)
    print(line(headers))
    print(separator)
    for row in rows:
        print(line(row))
    print(separator)
    print(line(footer))
    print('\nNull RS includes null_resource and terraform_data. Totals exclude duplicate and skipped files.')
    for row in report['files']:
        if row['reason']:
            print(row['status'].upper() + ': ' + json.dumps(row['file']) + ': ' + json.dumps(row['reason']))
        for warning in row['warnings']:
            print('WARNING: ' + json.dumps(row['file']) + ': ' + json.dumps(warning))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', type=Path, help='A raw state file or directory of *.tfstate files')
    parser.add_argument('-r', '--recursive', action='store_true', help='Scan subdirectories')
    parser.add_argument('--by-type', action='store_true', help='Show estimated RUM by resource type')
    parser.add_argument('--exclude', action='append', default=[], metavar='PATTERN',
                        help='Exclude matching names or relative paths (repeatable shell-style glob)')
    parser.add_argument('--csv', type=Path, help='Create a new CSV report (never overwrite)')
    parser.add_argument('--json', type=Path, help='Create a new JSON report (never overwrite)')
    args = parser.parse_args(argv)
    paths, errors, excluded = discover(args.path, args.recursive, args.exclude)
    if not paths:
        errors.append('No state files found')
    report = scan(paths, by_type=args.by_type)
    report['scan_errors'] = errors
    report['excluded_paths'] = excluded
    report['summary'] = {status: sum(row['status'] == status for row in report['files'])
                         for status in ('counted', 'duplicate', 'skipped')}
    report['summary'].update(scan_errors=len(errors), excluded_paths=len(excluded))
    print_table(report)
    for error in errors:
        print('SKIPPED: ' + json.dumps(error))
    summary = report['summary']
    print(f"Files: {summary['counted']} counted, {summary['duplicate']} duplicates ignored, "
          f"{summary['skipped']} skipped; {summary['scan_errors']} scan errors; "
          f"{summary['excluded_paths']} paths excluded.")
    if errors or summary['skipped']:
        print('PARTIAL ESTIMATE: some input could not be counted.')
    if excluded:
        print('Exclusions applied; excluded directories were not scanned.')
    if args.by_type:
        print('\nResource Type                         Estimated RUM')
        for kind, count in report['by_type'].items():
            label = json.dumps(kind, ensure_ascii=True)[1:-1]
            print(f'{label:<36}  {count:>13}')
        if not report['by_type']:
            print('(no counted resource instances)')
    print(f'Total estimated RUM: {report["total_rum"]} (supplied states only)')
    failed = bool(errors) or any(r['status'] == 'skipped' for r in report['files'])
    for path, as_csv in ((args.csv, True), (args.json, False)):
        if path is not None:
            if '.tfstate' in path.name.lower():
                print('ERROR: Report names must not contain .tfstate', file=sys.stderr)
                failed = True
                continue
            try:
                write_report(path, report, as_csv)
            except OSError:
                print('ERROR: Cannot create report (destination must be new and parent must exist): ' + json.dumps(str(path)), file=sys.stderr)
                failed = True
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
