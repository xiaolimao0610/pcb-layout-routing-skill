"""Standard-library HTTP client for the official EasyEDA bridge protocol.

This client never starts EasyEDA, a bridge, opens a project, or retries a write.
Only an explicitly requested method sends code to an already connected window.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4


class BridgeError(RuntimeError):
    pass


class EasyEDABridge:
    def __init__(self, base_url: str | None = None, timeout: float = 60.0,
                 adapter_path: str | Path | None = None):
        self.base_url = base_url.rstrip('/') if base_url else None
        self.timeout = timeout
        self.adapter_path = Path(adapter_path) if adapter_path else Path(__file__).with_name('eda_adapter.mjs')
        if self.base_url:
            parsed = urlparse(self.base_url)
            if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
                raise BridgeError('Only a loopback HTTP bridge is supported')
            if parsed.path not in ('', '/') or parsed.username or parsed.password:
                raise BridgeError('Bridge URL must be a bare loopback origin')

    def _request(self, path: str, payload: dict | None = None, *, base_url: str | None = None,
                 timeout: float | None = None):
        base = base_url or self.base_url
        if not base:
            raise BridgeError('Bridge has not been discovered')
        data = None if payload is None else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
        req = Request(base + path, data=data, headers={'Content-Type': 'application/json'})
        try:
            with urlopen(req, timeout=timeout or self.timeout) as response:
                result = json.loads(response.read().decode('utf-8'))
        except HTTPError as exc:
            # The gateway puts the actual adapter diagnostic in JSON on 4xx/5xx.
            # Preserve that bounded message; a bare HTTP 500 hides field failures.
            detail = str(exc)
            try:
                error = json.loads(exc.read(8192).decode('utf-8')).get('error')
                if isinstance(error, str):
                    detail += ': ' + error[:2000]
            except (ValueError, AttributeError, UnicodeError, OSError):
                pass
            raise BridgeError(f'{path} failed: {detail}. Writes are never retried; inspect native state and journal.') from exc
        except (URLError, TimeoutError, ValueError, OSError) as exc:
            raise BridgeError(f'{path} failed: {exc}. Writes are never retried; inspect native state and journal.') from exc
        if not isinstance(result, dict):
            raise BridgeError(f'{path}: expected JSON object')
        if result.get('error'):
            raise BridgeError(f'{path}: {result["error"]}')
        return result

    def health(self):
        if self.base_url is None:
            for port in range(49620, 49630):
                base = f'http://127.0.0.1:{port}'
                try:
                    response = self._request('/health', base_url=base, timeout=0.4)
                except BridgeError:
                    continue
                if response.get('service') == 'easyeda-bridge':
                    self.base_url = base
                    return response
            raise BridgeError('No existing easyeda-bridge on ports 49620–49629; start the official gateway yourself')
        response = self._request('/health')
        if response.get('service') != 'easyeda-bridge':
            raise BridgeError('Service identity mismatch; refusing to send code')
        return response

    def windows(self):
        self.health()
        return self._request('/eda-windows')

    def _window(self, window_id: str | None):
        health = self.health()
        if health.get('edaConnected') is not True:
            raise BridgeError('No EasyEDA window is connected to the gateway')
        info = self._request('/eda-windows')
        windows = [w for w in info.get('windows', []) if w.get('connected') is True]
        if window_id:
            if not any(w.get('windowId') == window_id for w in windows):
                raise BridgeError('Requested EasyEDA window is not connected')
            return window_id
        if len(windows) != 1:
            raise BridgeError('Pass window_id explicitly when multiple EasyEDA windows are connected')
        return windows[0]['windowId']

    def execute(self, code: str, window_id: str | None = None):
        """Execute once. A timeout has an unknown outcome and must not be retried."""
        if not isinstance(code, str) or not code.strip():
            raise BridgeError('Nonempty JavaScript code required')
        selected = self._window(window_id)
        response = self._request('/execute', {'code': code, 'windowId': selected})
        if 'result' not in response:
            raise BridgeError('Gateway omitted result; outcome is unknown')
        return response['result']

    def _adapter_code(self, expression: str):
        source = self.adapter_path.read_text(encoding='utf-8')
        # This repository-owned adapter has no imports or literal comment markers.
        source = re.sub(r'/\*.*?\*/', '', source, flags=re.S)
        source = re.sub(r'(?m)^\s*//[^\r\n]*', '', source)
        source = re.sub(r'\bexport\s+(?=(?:async\s+)?(?:function|class|const)\b)', '', source)
        if re.search(r'^\s*(?:import|export)\s', source, re.M):
            raise BridgeError('Adapter bundle contains unresolved module syntax')
        return 'return await (async()=>{' + source + '\nreturn await ' + expression + ';})();'

    def export_snapshot(self, context: dict | None = None, window_id: str | None = None):
        encoded = json.dumps(context or {}, ensure_ascii=False, allow_nan=False)
        return self.execute(self._adapter_code(f'exportBoard(eda,{encoded})'), window_id)

    def native_drc(self, document_uuid: str | None = None, window_id: str | None = None):
        encoded = json.dumps(document_uuid)
        return self.execute(self._adapter_code(f'runNativeDrc(eda,{encoded})'), window_id)

    def export_manufacturing(self, source: dict, output_dir: str | Path,
                             window_id: str | None = None, *, package_kind: str = 'gerber',
                             max_file_bytes: int = 33554432):
        """Export native handoff files into a new directory; never place an order."""
        if isinstance(source, dict) and source.get('usage') == 'test_only':
            raise BridgeError('test_only geometry cannot be used for manufacturing')
        if (isinstance(source, dict) and (set(source) - {'document_uuid', 'fingerprint', 'usage'}
                or source.get('usage') not in (None, 'physical'))):
            raise BridgeError('Unknown manufacturing source fields or usage')
        if (not isinstance(source, dict) or not isinstance(source.get('document_uuid'), str)
                or not source['document_uuid'] or not isinstance(source.get('fingerprint'), str)
                or not re.fullmatch(r'sha256:[0-9a-f]{64}', source['fingerprint'])):
            raise BridgeError('Manufacturing requires a live document UUID and native SHA-256 fingerprint')
        if type(max_file_bytes) is not int or not 0 < max_file_bytes <= 67108864:
            raise BridgeError('max_file_bytes must be 1..67108864')
        directory = Path(output_dir)
        if directory.exists() or directory.is_symlink():
            raise BridgeError('Manufacturing output directory must be new')
        directory.mkdir(parents=True, exist_ok=False)
        receipt = directory / 'manufacturing-receipt.json'
        payload = {'success': False, 'status': 'pending', 'source': source,
                   'package_kind': package_kind, 'files': []}
        written = []
        try:
            receipt.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
            options = {'source': source, 'package_kind': package_kind, 'max_file_bytes': max_file_bytes}
            result = self.execute(self._adapter_code('exportManufacturing(eda,' +
                                  json.dumps(options, ensure_ascii=False, allow_nan=False) + ')'), window_id)
            if not isinstance(result, dict) or type(result.get('success')) is not bool:
                raise BridgeError('Manufacturing adapter omitted explicit success flag')
            if result['success'] is not True:
                payload.update(status='failed', result=result)
                return {**result, 'local_receipt_file': str(receipt.resolve())}
            drc = result.get('drc', {})
            if result.get('source') != source or not isinstance(drc, dict) or drc.get('passed') is not True or drc.get('issues') != []:
                raise BridgeError('Manufacturing source or zero-issue native DRC receipt mismatch')
            files = result.get('files')
            if not isinstance(files, list) or len(files) != 3 or {f.get('kind') for f in files if isinstance(f, dict)} != {'fabrication', 'bom', 'pick_and_place'}:
                raise BridgeError('Expected fabrication, BOM and pick-and-place files')
            decoded, names = [], {'manufacturing-receipt.json'}
            for item in files:
                name, size = item.get('name'), item.get('size')
                if (not isinstance(name, str) or not name or len(name) > 180 or name in ('.', '..')
                        or re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name.endswith((' ', '.'))
                        or re.fullmatch(r'(?i)(?:con(?:in\$|out\$)?|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])[ .]*(?:\..*)?', name)
                        or name.casefold() in names):
                    raise BridgeError('Unsafe or duplicate native file name')
                names.add(name.casefold())
                if type(size) is not int or not 0 < size <= max_file_bytes:
                    raise BridgeError('Invalid manufacturing file size')
                encoded = item.get('data_base64')
                if not isinstance(encoded, str) or len(encoded) != 4 * ((size + 2) // 3):
                    raise BridgeError('Invalid manufacturing base64 length')
                try:
                    raw = base64.b64decode(encoded, validate=True)
                except ValueError as exc:
                    raise BridgeError('Invalid manufacturing base64') from exc
                digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
                if len(raw) != size or item.get('sha256') != digest:
                    raise BridgeError('Manufacturing byte length or SHA-256 mismatch')
                destination = directory / name
                if destination.resolve().parent != directory.resolve():
                    raise BridgeError('Native file escapes output directory')
                decoded.append((item, destination, raw))
            for item, destination, raw in decoded:
                with destination.open('xb') as handle:
                    written.append(destination)
                    handle.write(raw)
                payload['files'].append({k: item.get(k) for k in ('kind', 'name', 'size', 'sha256', 'mime_type')})
            payload.update(success=True, status='exported', source=result['source'], drc=drc,
                           scope=result.get('scope'), settings=result.get('settings'), live_board_validated=False)
            return {**payload, 'local_output_dir': str(directory.resolve()),
                    'local_receipt_file': str(receipt.resolve())}
        except Exception as exc:
            for path in written:
                path.unlink(missing_ok=True)
            payload.update(success=False, status='failed', files=[], error=str(exc))
            raise
        finally:
            receipt.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

    def apply_job_status(self, job_key: str, window_id: str | None = None):
        """Read a dispatched transaction; this never repeats its native writes."""
        if not isinstance(job_key, str) or not re.fullmatch(r'pcb_pipeline:apply_job:[A-Za-z0-9._:-]{1,160}', job_key):
            raise BridgeError('Invalid background job key')
        expression = 'readApplyJob(eda,' + json.dumps(job_key) + ')'
        return self.execute(self._adapter_code(expression), window_id)

    def wait_apply_job(self, job_key: str, window_id: str | None = None, *,
                       max_wait_seconds: float = 600.0, poll_seconds: float = 1.0,
                       plan_id: str | None = None, document_uuid: str | None = None):
        if any(isinstance(n, bool) or not isinstance(n, (int, float))
               or not math.isfinite(n) or n <= 0 for n in (max_wait_seconds, poll_seconds)):
            raise BridgeError('Background wait and polling intervals must be finite positive seconds')
        started = time.monotonic()
        while True:
            record = self.apply_job_status(job_key, window_id)
            if not isinstance(record, dict) or record.get('job_key') != job_key:
                raise BridgeError('Background job receipt missing or mismatched; outcome unknown')
            if ((plan_id is not None and record.get('plan_id') != plan_id)
                    or (document_uuid is not None and record.get('document_uuid') != document_uuid)):
                raise BridgeError('Background job source/plan mismatch; outcome unknown')
            if record.get('status') == 'final':
                result = record.get('result')
                if not isinstance(result, dict) or type(result.get('success')) is not bool:
                    raise BridgeError('Background job omitted final result; outcome unknown')
                return result
            if record.get('status') != 'pending':
                raise BridgeError('Unknown background job status; outcome unknown')
            remaining = max_wait_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise BridgeError('Background wait timed out; task may still be running. Read job status; do not redispatch: ' + job_key)
            time.sleep(min(poll_seconds, remaining))

    def apply_plan(self, plan: dict, window_id: str | None = None, *, context: dict | None = None,
                   save: bool = False, audit_dir: str | Path | None = None,
                   background: bool = False, max_wait_seconds: float = 600.0):
        """Back up native source locally, then apply once with an extension journal.

        Local receipt survives even if the HTTP response is lost. A transport error
        records outcome_unknown; it does not imply that the native mutation failed.
        """
        if type(background) is not bool:
            raise BridgeError('background must be boolean')
        if background and (isinstance(max_wait_seconds, bool) or not isinstance(max_wait_seconds, (int, float))
                           or not math.isfinite(max_wait_seconds) or max_wait_seconds <= 0):
            raise BridgeError('Background wait must be finite positive seconds')
        if (plan.get('source', {}).get('usage') == 'test_only'
                or plan.get('target_board', {}).get('metadata', {}).get('board_usage') == 'test_only'
                or plan.get('target_board', {}).get('metadata', {}).get('source', {}).get('usage') == 'test_only'
                or (context or {}).get('board', {}).get('usage') == 'test_only'):
            raise BridgeError('test_only geometry cannot be applied to a live document')
        selected = self._window(window_id)
        directory = Path(audit_dir) if audit_dir else Path.cwd() / 'pcb_apply_logs'
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '_' + uuid4().hex[:12]
        receipt = directory / (stamp + '.receipt.json')
        backup = directory / (stamp + '.source.txt')
        native = self.execute('return {document: await eda.dmt_SelectControl.getCurrentDocumentInfo(), source: await eda.sys_FileManager.getDocumentSource()};', selected)
        if not isinstance(native, dict) or not isinstance(native.get('source'), str):
            raise BridgeError('Cannot capture native source backup; refusing apply')
        expected = plan.get('source', {}).get('document_uuid')
        if native.get('document', {}).get('uuid') != expected:
            raise BridgeError('Backup document UUID differs from plan')
        backup.write_text(native['source'], encoding='utf-8', newline='')
        payload = {'status': 'pending', 'window_id': selected, 'backup_file': str(backup.resolve()), 'plan': plan}
        receipt.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        try:
            options = {'context': context or {}, 'save': save}
            expression = 'applyPlan(eda,' + json.dumps(plan, ensure_ascii=False, allow_nan=False) + ',' + json.dumps(options, ensure_ascii=False, allow_nan=False) + ')'
            if background:
                job_key = 'pcb_pipeline:apply_job:' + uuid4().hex
                payload.update(status='dispatching', background_job_key=job_key)
                receipt.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
                expression = ('startApplyJob(eda,' + json.dumps(plan, ensure_ascii=False, allow_nan=False)
                              + ',' + json.dumps(options, ensure_ascii=False, allow_nan=False)
                              + ',' + json.dumps(job_key) + ')')
                acknowledgement = self.execute(self._adapter_code(expression), selected)
                if not isinstance(acknowledgement, dict) or acknowledgement.get('job_key') != job_key:
                    raise BridgeError('Background dispatch acknowledgement missing; outcome unknown')
                payload.update(status='running', acknowledgement=acknowledgement)
                receipt.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
                result = self.wait_apply_job(job_key, selected, max_wait_seconds=max_wait_seconds,
                                             plan_id=plan.get('plan_id'), document_uuid=expected)
            else:
                result = self.execute(self._adapter_code(expression), selected)
            if not isinstance(result, dict) or type(result.get('success')) is not bool:
                raise BridgeError('Adapter omitted explicit success flag; outcome unknown')
            payload.update(status='completed' if result['success'] else 'failed', result=result)
            result['local_backup_file'] = str(backup.resolve())
            result['local_receipt_file'] = str(receipt.resolve())
            return result
        except Exception as exc:
            payload.update(status='outcome_unknown', error=str(exc))
            raise
        finally:
            receipt.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


# Short alias for callers that prefer the generic name.
BridgeClient = EasyEDABridge


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url')
    parser.add_argument('--window-id')
    parser.add_argument('--timeout', type=float, default=60)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('health')
    sub.add_parser('windows')
    export = sub.add_parser('export')
    export.add_argument('--context', type=Path, required=True)
    export.add_argument('--output', type=Path, required=True)
    drc = sub.add_parser('drc')
    drc.add_argument('--document-uuid')
    drc.add_argument('--output', type=Path)
    apply = sub.add_parser('apply')
    apply.add_argument('--plan', type=Path, required=True)
    apply.add_argument('--context', type=Path, required=True)
    apply.add_argument('--audit-dir', type=Path, required=True)
    apply.add_argument('--output', type=Path, required=True)
    apply.add_argument('--save', action='store_true')
    apply.add_argument('--background', action='store_true', help='Managed EDA task; avoids the gateway 30-second response limit')
    apply.add_argument('--max-wait-seconds', type=float, default=600)
    job = sub.add_parser('job-status')
    job.add_argument('--job-key', required=True)
    job.add_argument('--output', type=Path)
    manufacture = sub.add_parser('manufacture')
    manufacture.add_argument('--source-snapshot', type=Path,
                             help='Read metadata.source and retain test_only provenance')
    manufacture.add_argument('--document-uuid')
    manufacture.add_argument('--fingerprint')
    manufacture.add_argument('--output-dir', type=Path, required=True)
    manufacture.add_argument('--package-kind', choices=('gerber', 'manufacture_data'), default='gerber')
    args = parser.parse_args(argv)
    client = EasyEDABridge(args.base_url, args.timeout)
    read = lambda path: json.loads(path.read_text(encoding='utf-8-sig'))
    try:
        if args.command == 'health': result = client.health()
        elif args.command == 'windows': result = client.windows()
        elif args.command == 'export': result = client.export_snapshot(read(args.context), args.window_id)
        elif args.command == 'drc': result = client.native_drc(args.document_uuid, args.window_id)
        elif args.command == 'job-status': result = client.apply_job_status(args.job_key, args.window_id)
        elif args.command == 'manufacture':
            if args.source_snapshot:
                if args.document_uuid or args.fingerprint:
                    raise BridgeError('Use source-snapshot or document-uuid/fingerprint, not both')
                snapshot = read(args.source_snapshot)
                if not isinstance(snapshot, dict):
                    raise BridgeError('Manufacturing snapshot must be an object')
                metadata = snapshot.get('metadata', {})
                if not isinstance(metadata, dict):
                    raise BridgeError('Manufacturing snapshot metadata must be an object')
                if metadata.get('preview_only') is True:
                    raise BridgeError('test_only preview geometry cannot be used for manufacturing')
                if not isinstance(metadata.get('source', {}), dict):
                    raise BridgeError('Manufacturing snapshot source must be an object')
                source = dict(metadata.get('source', {}))
                if metadata.get('board_usage') == 'test_only':
                    source['usage'] = 'test_only'
                if metadata.get('board_usage') not in (None, 'physical', 'test_only'):
                    raise BridgeError('Unknown board usage in manufacturing snapshot')
            else:
                if not args.document_uuid or not args.fingerprint:
                    raise BridgeError('Manufacturing needs --source-snapshot or both document-uuid/fingerprint')
                source = {'document_uuid': args.document_uuid, 'fingerprint': args.fingerprint}
            result = client.export_manufacturing(source, args.output_dir, args.window_id,
                                                 package_kind=args.package_kind)
        else: result = client.apply_plan(read(args.plan), args.window_id, context=read(args.context), save=args.save, audit_dir=args.audit_dir,
                                        background=args.background, max_wait_seconds=args.max_wait_seconds)
        serialized = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
        if getattr(args, 'output', None): args.output.write_text(serialized + '\n', encoding='utf-8')
        else: print(serialized)
        if (args.command == 'job-status' and isinstance(result, dict) and result.get('status') == 'final'
                and isinstance(result.get('result'), dict) and result['result'].get('success') is False):
            return 1
        return 1 if isinstance(result, dict) and (result.get('success') is False or result.get('passed') is False) else 0
    except (BridgeError, OSError, ValueError) as exc:
        print(json.dumps({'success': False, 'error': str(exc)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
