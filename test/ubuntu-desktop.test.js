import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { join } from 'node:path';

test('Ubuntu tiling reports the real outer window box and excludes desktop panels', () => {
  const helper = join(import.meta.dirname, '../image/ubuntu/rootfs/usr/local/bin/mola-tile');
  // Recorded XFCE canary values: wmctrl reports (730,85), but X's actual
  // client is at (725,56), inside a frame whose origin is (720,27).
  const result = spawnSync('python3', ['-c', String.raw`
import importlib.util, importlib.machinery, json, sys
sys.dont_write_bytecode = True
loader = importlib.machinery.SourceFileLoader('tile', sys.argv[1])
spec = importlib.util.spec_from_loader(loader.name, loader)
tile = importlib.util.module_from_spec(spec); loader.exec_module(tile)
def command(*args):
    if args == ('wmctrl', '-lG'):
        return '0x00000001 0 730 85 707 825 host Terminal\n0x00000002 -1 0 0 1440 27 host Panel\n'
    if args == ('xprop', '-id', '0x00000001', '_NET_WM_WINDOW_TYPE'):
        return '_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_NORMAL'
    if args == ('xprop', '-id', '0x00000002', '_NET_WM_WINDOW_TYPE'):
        return '_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_DOCK'
    if args == ('xprop', '-id', '0x00000001', '_NET_FRAME_EXTENTS'):
        return '_NET_FRAME_EXTENTS(CARDINAL) = 5, 5, 29, 5'
    if args == ('xwininfo', '-id', '0x00000001'):
        return '  Absolute upper-left X: 725\n  Absolute upper-left Y: 56\n  Width: 707\n  Height: 825\n'
    raise AssertionError(args)
tile.command = command
print(json.dumps(tile.windows()))
`, helper], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), [{
    id: '0x00000001', desktop: 0, x: 720, y: 27,
    width: 717, height: 859, title: 'Terminal',
  }]);
});
