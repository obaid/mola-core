"""Isolated XRes/identity fixtures: no X server, guest, or customer data."""
import base64
import ctypes
import importlib.util
import pathlib
import struct
import sys
import unittest
import zlib
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('tools',sys.argv[1]);g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)
IDENTITY={'window_id':'0x12ab','pid':42,'start_ticks':'98765','boot_id':'e1019c8c-7c38-4844-a8d4-2d140a2c261f','wm_class':['fixture','Fixture'],'instance_nonce':'a'*64}

class FakeConnection:
    trace=[]; stale=False
    def __init__(self,window):self.window=window;self.trace.append('connect')
    def __enter__(self):self.trace.append('grab');return self
    def __exit__(self,*_):self.trace.append('ungrab')
    def verify(self,identity):
        self.trace.append('verify')
        g.require(identity==IDENTITY and not self.stale,'window_identity_changed')
    def identity(self,**_):return IDENTITY.copy()
    def bounds(self):return {'width':2,'height':1}
    def pixels(self,crop):
        self.trace.append('pixels');return bytes([0,0,255,0,0,255,0,0]),8,32,0,[0xff0000,0xff00,0xff]
    def event(self,kind,*_):self.trace.append(('event',kind))

class Fixture(unittest.TestCase):
    def setUp(self):FakeConnection.trace=[];FakeConnection.stale=False
    def test_missing_library_fails_without_spoofable_property_fallback(self):
        with patch.object(g.ctypes,'CDLL',side_effect=OSError()),patch.object(g,'command') as command:
            with self.assertRaises(g.ToolError) as result:g.window_identity({'window_id':'0x12ab'})
            self.assertEqual(result.exception.status,501);command.assert_not_called()
    def test_invalid_xid_is_not_loaded(self):
        for value in ['0x0','0x123456789','other',None]:
            with self.assertRaises(g.ToolError):g.WindowConnection(value)
    def test_png_encoding_after_ungrab_and_cropped_pixels_only(self):
        original=g.window_png
        def encode(*args):
            self.assertEqual(FakeConnection.trace[-1],'ungrab');return original(*args)
        with patch.object(g,'WindowConnection',FakeConnection),patch.object(g,'window_png',side_effect=encode),patch.object(g,'command') as command:
            image=g.capture({'mode':'window','window_id':'0x12Ab','window_identity':IDENTITY})
            command.assert_not_called()
        self.assertEqual(FakeConnection.trace,['connect','grab','verify','pixels','verify','ungrab'])
        png=base64.b64decode(image['image_base64']);self.assertEqual(png[:8],b'\x89PNG\r\n\x1a\n')
        chunks={};offset=8
        while offset<len(png):
            length=struct.unpack('!I',png[offset:offset+4])[0];kind=png[offset+4:offset+8]
            chunks[kind]=png[offset+8:offset+8+length];offset+=length+12
        self.assertEqual(zlib.decompress(chunks[b'IDAT']),b'\0\xff\0\0\0\xff\0')
    def test_reused_window_before_capture_returns_no_pixels(self):
        FakeConnection.stale=True
        with patch.object(g,'WindowConnection',FakeConnection),patch.object(g,'window_png') as encode:
            with self.assertRaises(g.ToolError):g.scoped_window_capture({'window_id':'0x12ab','window_identity':IDENTITY})
            encode.assert_not_called()
        self.assertNotIn('pixels',FakeConnection.trace);self.assertEqual(FakeConnection.trace[-1],'ungrab')
    def test_changed_identity_after_capture_discards_pixels(self):
        class Changed(FakeConnection):
            def pixels(self,crop):
                result=super().pixels(crop);self.stale=True;return result
        with patch.object(g,'WindowConnection',Changed),patch.object(g,'window_png') as encode:
            with self.assertRaises(g.ToolError):g.scoped_window_capture({'window_id':'0x12ab','window_identity':IDENTITY})
            encode.assert_not_called()
        self.assertEqual(FakeConnection.trace[-1],'ungrab')
    def test_direct_window_events_do_not_move_global_pointer(self):
        with patch.object(g,'WindowConnection',FakeConnection),patch.object(g,'command') as command:
            result=g.view_input({'scope':{'mode':'window','window_id':'0x12ab','window_identity':IDENTITY},'input':{'action':'click','x':1,'y':0}})
            self.assertTrue(result['success']);command.assert_not_called()
        self.assertEqual(FakeConnection.trace,['connect','grab','verify','verify',('event',4),('event',5),'verify','ungrab'])
    def test_missing_identity_and_keyboard_do_not_dispatch(self):
        with patch.object(g,'WindowConnection',FakeConnection),patch.object(g,'command') as command:
            with self.assertRaises(g.ToolError) as missing:g.view_input({'mode':'window','window_id':'0x12ab','action':'click','x':0,'y':0})
            self.assertEqual(missing.exception.code,'window_identity_required')
            with self.assertRaises(g.ToolError) as result:g.view_input({'mode':'window','window_id':'0x12ab','window_identity':IDENTITY,'action':'key','key':'Return'})
            self.assertEqual(result.exception.status,501);command.assert_not_called()
    def test_pid_reuse_boot_change_and_class_change_are_rejected(self):
        for field,value in [('pid',43),('start_ticks','98766'),('boot_id','e1019c8c-7c38-4844-a8d4-2d140a2c261e'),('wm_class',['other','Other']),('instance_nonce','b'*64)]:
            connection=g.WindowConnection.__new__(g.WindowConnection)
            with patch.object(connection,'identity',return_value={**IDENTITY,field:value}):
                with self.assertRaises(g.ToolError):connection.verify(IDENTITY)
    def test_session_windows_preserves_geometry_and_no_title_in_identity(self):
        with patch.object(g,'WindowConnection',FakeConnection),patch.object(g,'windows',return_value=[{'id':'0x12ab','x':0,'y':0,'width':2,'height':1}]):
            windows=g.session_windows();self.assertEqual(windows[0]['wm_class'],IDENTITY['wm_class'])
            self.assertNotIn('title',windows[0]);self.assertNotIn('pid',windows[0])
    def test_identity_admission_probes_before_issuance_and_returns_no_pixels(self):
        minted=[]
        class Ready(FakeConnection):
            def identity(self,**kwargs):minted.append(kwargs.get('create_instance',False));return IDENTITY.copy()
            def probe_offscreen(self):self.trace.append('probe')
        with patch.object(g,'WindowConnection',Ready),patch.object(g,'command') as command:
            result=g.window_identity({'window_id':'0x12ab'})
            self.assertEqual(result,IDENTITY);command.assert_not_called()
        self.assertEqual(minted,[False,True]);self.assertIn('probe',FakeConnection.trace)
    def test_missing_offscreen_buffer_prevents_identity_nonce_issuance(self):
        minted=[]
        class Unsupported(FakeConnection):
            def identity(self,**kwargs):minted.append(kwargs.get('create_instance',False));return IDENTITY.copy()
            def probe_offscreen(self):raise g.ToolError('window_capture_unsupported',501)
        with patch.object(g,'WindowConnection',Unsupported):
            with self.assertRaises(g.ToolError) as error:g.window_identity({'window_id':'0x12ab'})
        self.assertEqual(error.exception.status,501);self.assertEqual(minted,[False])
    def test_compositor_preparation_requires_explicit_consent_before_any_change(self):
        for value in [None,False,'true',1]:
            with patch.object(g,'command') as command:
                with self.assertRaises(g.ToolError):g.window_prepare({'window_id':'0x12ab','allow_compositor':value})
                command.assert_not_called()
    def test_compositor_preparation_sets_only_known_xfce_boolean_then_proves_buffer(self):
        class Ready(FakeConnection):
            def probe_offscreen(self):self.trace.append('probe')
        with patch.object(g,'WindowConnection',Ready),patch.object(g,'command',side_effect=['100','false','']) as command,patch.object(g.os,'readlink',return_value='/usr/bin/xfwm4'):
            self.assertEqual(g.window_prepare({'window_id':'0x12ab','allow_compositor':True}),{'success':True})
            self.assertEqual(command.call_args_list[2].args[0],['/usr/bin/xfconf-query','-c','xfwm4','-p','/general/use_compositing','-s','true'])
        self.assertIn('probe',FakeConnection.trace)
    def test_unsupported_desktop_never_changes_config(self):
        with patch.object(g,'WindowConnection',FakeConnection),patch.object(g,'command',side_effect=['100','unknown']) as command,patch.object(g.os,'readlink',return_value='/usr/bin/xfwm4'):
            with self.assertRaises(g.ToolError) as error:g.window_prepare({'window_id':'0x12ab','allow_compositor':True})
            self.assertEqual(error.exception.status,501);self.assertEqual(command.call_count,2)
    def composite_connection(self, available=True):
        connection=g.WindowConnection.__new__(g.WindowConnection);connection.display=1;connection.window=0x12ab;connection.errors=False
        connection.border=2;connection.client_dimensions=(2,1)
        class Method:
            def __init__(self,fn):self.fn=fn
            def __call__(self,*args):return self.fn(*args)
        def version(display,major,minor):
            ctypes.cast(major,ctypes.POINTER(ctypes.c_int))[0]=0;ctypes.cast(minor,ctypes.POINTER(ctypes.c_int))[0]=4;return 1
        def name(display,window):
            assert window==0x12ab
            if not available:connection.errors=True
            return 999
        class Composite:pass
        composite=Composite();composite.XCompositeQueryVersion=Method(version);composite.XCompositeNameWindowPixmap=Method(name)
        class X:
            def __init__(self):self.requests=[];self.freed=[];self.destroyed=0
            buffer=ctypes.create_string_buffer(bytes([0,0,255,0,0,255,0,0]))
            def XSync(self,*_):pass
            def XQueryExtension(self,display,name,opcode,event,error):
                ctypes.cast(opcode,ctypes.POINTER(ctypes.c_int))[0]=142;return 1
            def XDefaultRootWindow(self,*_):return 1
            def XGetImage(self,display,drawable,x,y,width,height,planes,format):
                # Distinct overlapping-window pixels can never be requested:
                # only the named offscreen buffer, offset past client border.
                assert drawable==999 and (x,y,width,height)==(2,2,2,1)
                self.requests.append(drawable)
                image=g.WindowConnection.Image();image.width=2;image.height=1;image.data=ctypes.addressof(self.buffer)
                image.bits_per_pixel=32;image.byte_order=0;image.bytes_per_line=8
                image.red_mask=0xff0000;image.green_mask=0xff00;image.blue_mask=0xff
                self.image=image;return ctypes.pointer(self.image)
            def XDestroyImage(self,*_):self.destroyed+=1
            def XFreePixmap(self,display,pixmap):self.freed.append(pixmap)
        connection.x=X();return connection,composite
    def ancestor_connection(self, overlapping=False, missing_child=False, geometry_changed=False):
        connection,composite=self.composite_connection();connection.failures=[]
        original_name=composite.XCompositeNameWindowPixmap
        def name(display,window):
            if window==0x12ab:
                connection.errors=True;connection.failures.append((8,142,6));return 998
            assert window==2;return 999
        original_name.fn=name
        trees={0x12ab:(1,2,[]),2:(1,1,([3] if missing_child else [0x12ab,3]))}
        def tree(window):return trees[window]
        def geometry(window):
            if window==2:return 12,8,1
            if window==3:return 12,2,0
            if window==999:return (15 if geometry_changed else 14),10,0
            raise AssertionError('unexpected geometry')
        def translate(source,target):
            if (source,target)==(0x12ab,2):return 3,4
            if (source,target)==(3,0x12ab):return -3,(-1 if overlapping else -4)
            raise AssertionError('unexpected translation')
        connection.tree=tree;connection.drawable_geometry=geometry;connection.translate=translate
        return connection,composite
    def test_reparented_client_uses_only_translated_client_rectangle_not_frame_border(self):
        connection,composite=self.ancestor_connection()
        original=connection.x.XGetImage
        def image(display,drawable,x,y,*arguments):
            self.assertEqual((drawable,x,y),(999,4,5))
            return original(display,drawable,2,2,*arguments)
        with patch.object(g.ctypes,'CDLL',return_value=composite),patch.object(connection.x,'XGetImage',side_effect=image):
            connection.pixels({'x':0,'y':0,'width':2,'height':1})
        self.assertEqual(connection.capture_offset,(4,5))
        self.assertEqual(connection.x.freed,[999])
        self.assertFalse(connection.errors)
    def test_intersecting_nonpath_sibling_refuses_before_any_frame_read(self):
        connection,composite=self.ancestor_connection(overlapping=True)
        with patch.object(g.ctypes,'CDLL',return_value=composite):
            with self.assertRaises(g.ToolError):connection.offscreen_pixmap()
        self.assertEqual(connection.x.requests,[])
    def test_reparented_missing_child_relation_fails_closed(self):
        connection,composite=self.ancestor_connection(missing_child=True)
        with patch.object(g.ctypes,'CDLL',return_value=composite):
            with self.assertRaises(g.ToolError) as error:connection.offscreen_pixmap()
        self.assertEqual(error.exception.code,'window_identity_changed');self.assertEqual(connection.x.requests,[])
    def test_ancestor_pixmap_geometry_race_discards_buffer(self):
        connection,composite=self.ancestor_connection(geometry_changed=True)
        with patch.object(g.ctypes,'CDLL',return_value=composite):
            with self.assertRaises(g.ToolError):connection.offscreen_pixmap()
        self.assertEqual(connection.x.requests,[]);self.assertEqual(connection.x.freed,[999])
    def test_root_and_cyclic_ancestor_are_never_named(self):
        for parent in [1,0,0x12ab]:
            connection,composite=self.ancestor_connection()
            connection.tree=lambda _: (1,parent,[])
            with patch.object(g.ctypes,'CDLL',return_value=composite):
                with self.assertRaises(g.ToolError):connection.offscreen_pixmap()
            self.assertEqual(connection.x.requests,[])
    def test_other_x_errors_never_advance_to_parent(self):
        connection,composite=self.ancestor_connection()
        def missing(display,window):
            connection.errors=True;connection.failures.append((3,142,6));return 998
        composite.XCompositeNameWindowPixmap.fn=missing
        with patch.object(g.ctypes,'CDLL',return_value=composite),patch.object(connection,'tree') as tree:
            with self.assertRaises(g.ToolError):connection.offscreen_pixmap()
            tree.assert_not_called()
    def test_capture_reads_named_pixmap_client_area_and_releases_resources(self):
        connection,composite=self.composite_connection()
        with patch.object(g.ctypes,'CDLL',return_value=composite),patch.object(connection,'drawable_geometry',return_value=(6,5,0)):
            raw=connection.pixels({'x':0,'y':0,'width':2,'height':1})
        self.assertEqual(raw[0],bytes([0,0,255,0,0,255,0,0]));self.assertEqual(connection.x.requests,[999])
        self.assertEqual(connection.x.freed,[999]);self.assertEqual(connection.x.destroyed,1)
    def test_missing_redirected_buffer_never_falls_back_to_window_pixels(self):
        connection,composite=self.composite_connection(False)
        with patch.object(g.ctypes,'CDLL',return_value=composite):
            with self.assertRaises(g.ToolError) as error:connection.pixels({'x':0,'y':0,'width':2,'height':1})
        self.assertEqual(error.exception.status,501);self.assertEqual(connection.x.requests,[])
    def test_offscreen_geometry_mismatch_discards_pixmap(self):
        connection,composite=self.composite_connection()
        with patch.object(g.ctypes,'CDLL',return_value=composite),patch.object(connection,'drawable_geometry',return_value=(7,5,0)):
            with self.assertRaises(g.ToolError):connection.pixels({'x':0,'y':0,'width':2,'height':1})
        self.assertEqual(connection.x.requests,[]);self.assertEqual(connection.x.freed,[999])
    def property_connection(self, value=None, kind=31, format=8, after=0):
        connection=g.WindowConnection.__new__(g.WindowConnection);connection.display=1;connection.window=0x12ab;connection.errors=False
        class X:
            property=value;writes=0;buffers=[]
            def XInternAtom(self,display,name,only):
                assert name==b'_MOLA_VIEWER_INSTANCE' and only==0;return 100
            def XGetWindowProperty(self,display,window,atom,offset,length,delete,type,k,f,n,a,data):
                assert window==0x12ab and atom==100 and length==17 and offset==0 and delete==0 and type==31
                ctypes.cast(k,ctypes.POINTER(ctypes.c_ulong))[0]=0 if self.property is None else kind
                ctypes.cast(f,ctypes.POINTER(ctypes.c_int))[0]=format
                ctypes.cast(n,ctypes.POINTER(ctypes.c_ulong))[0]=0 if self.property is None else len(self.property)
                ctypes.cast(a,ctypes.POINTER(ctypes.c_ulong))[0]=after
                if self.property is not None:
                    buffer=ctypes.create_string_buffer(self.property);self.buffers.append(buffer)
                    ctypes.cast(data,ctypes.POINTER(ctypes.c_void_p))[0]=ctypes.addressof(buffer)
                return 0
            def XChangeProperty(self,display,window,atom,type,format,mode,data,length):
                assert (window,atom,type,format,mode,length)==(0x12ab,100,31,8,0,64)
                self.property=ctypes.string_at(data,length);self.writes+=1;return 1
            def XSync(self,*_):pass
            def XFree(self,*_):pass
        connection.x=X();return connection
    def test_issuance_mints_once_then_preserves_existing_nonce(self):
        connection=self.property_connection()
        with patch.object(g.os,'urandom',return_value=bytes([7])*32):
            self.assertEqual(connection.instance(True),'07'*32)
            self.assertEqual(connection.instance(True),'07'*32)
        self.assertEqual(connection.x.writes,1)
    def test_capture_never_mints_missing_property(self):
        connection=self.property_connection()
        self.assertIsNone(connection.instance(False));self.assertEqual(connection.x.writes,0)
    def test_recreated_same_process_window_loses_nonce(self):
        connection=g.WindowConnection.__new__(g.WindowConnection)
        with patch.object(connection,'identity',return_value={k:v for k,v in IDENTITY.items() if k!='instance_nonce'}):
            with self.assertRaises(g.ToolError):connection.verify(IDENTITY)
    def test_malformed_property_never_overwritten(self):
        for value,kind,format,after in [(b'a'*63,31,8,0),(b'a'*64,32,8,0),(b'a'*64,31,32,0),(b'a'*64,31,8,1),(b'z'*64,31,8,0)]:
            connection=self.property_connection(value,kind,format,after)
            with self.assertRaises(g.ToolError):connection.instance(True)
            self.assertEqual(connection.x.writes,0)
    def test_xres_server_pid_and_proc_start_tick_are_the_only_process_proof(self):
        connection=g.WindowConnection.__new__(g.WindowConnection);connection.display=1;connection.window=0x12ab;connection.errors=False
        class Value(ctypes.Structure):
            _fields_=[('spec',g.WindowConnection.Spec),('length',ctypes.c_long),('value',ctypes.c_void_p)]
        connection.Value=Value;values=(Value*1)();values[0].spec=g.WindowConnection.Spec(0x12ab,2)
        names=[ctypes.create_string_buffer(b'fixture'),ctypes.create_string_buffer(b'Fixture')]
        class Res:
            def XResQueryClientIds(self,display,n,spec,count,result):
                self.assert_spec=ctypes.cast(spec,ctypes.POINTER(g.WindowConnection.Spec)).contents
                assert self.assert_spec.client==0x12ab and self.assert_spec.mask==2
                ctypes.cast(count,ctypes.POINTER(ctypes.c_long))[0]=1
                ctypes.cast(result,ctypes.POINTER(ctypes.POINTER(Value)))[0]=ctypes.cast(values,ctypes.POINTER(Value));return 0
            def XResGetClientPid(self,value):return 42
            def XResClientIdsDestroy(self,*_):pass
        class X:
            def XGetClassHint(self,display,window,hint):
                value=ctypes.cast(hint,ctypes.POINTER(g.WindowConnection.ClassHint)).contents
                value.name=ctypes.addressof(names[0]);value.klass=ctypes.addressof(names[1]);return 1
            def XFree(self,*_):pass
            def XSync(self,*_):pass
        connection.res=Res();connection.x=X()
        stat='42 (a name with ) parentheses) S '+' '.join(['0']*18+['98765'])
        with patch.object(g.Path,'read_text',side_effect=[stat,IDENTITY['boot_id']]),patch.object(g,'command') as command:
            with patch.object(connection,'instance',return_value='a'*64):
                self.assertEqual(connection.identity(),IDENTITY);command.assert_not_called()

unittest.main(argv=[sys.argv[0]])
