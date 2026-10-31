"""Local development pairing/relay for a real audio receiver (not a party).
Bound by serve.py to loopback. Phone access uses an explicit USB reverse tunnel.
"""
import json, secrets, time, threading, queue, socket, re

LOCK=threading.RLock()
DEVICES={}; PAIRS={}; TOKENS={}; ATTEMPTS=[]; ARTWORKS={}
PROTOCOL='aleztesia.connect.v1'

def local(handler):
    host = handler.headers.get('Host', '')
    # Accept loopback (existing behavior) or any private-network host
    if host in ('127.0.0.1:8766', 'localhost:8766'):
        return True
    # Accept <ip>:8766 where ip is a private/link-local address
    h = host.rsplit(':', 1)[0] if ':' in host else host
    try:
        import ipaddress
        addr = ipaddress.ip_address(h)
        return addr.is_private or addr.is_loopback
    except ValueError:
        return False
def browser(handler):
    if not local(handler):
        return False
    origin = handler.headers.get('Origin', '')
    host = handler.headers.get('Host', '')
    # Browser: Origin matches the Host header (scheme doesn't matter for dev)
    return origin in (f'http://{host}', f'https://{host}')
def native(handler): return local(handler) and not handler.headers.get('Origin')
def reply(handler,status,data):
    raw=json.dumps(data).encode();handler.send_response(status);handler.send_header('Content-Type','application/json');handler.send_header('Content-Length',str(len(raw)));handler.send_header('Cache-Control','no-store');handler.end_headers();handler.wfile.write(raw)
def body(handler):
    size=int(handler.headers.get('Content-Length','0'))
    if not 0<size<2_000_000: raise ValueError('invalid body size')
    data=json.loads(handler.rfile.read(size))
    if not isinstance(data,dict): raise ValueError('invalid body')
    return data
def bearer(handler): return handler.headers.get('Authorization','').removeprefix('Bearer ')
def public(device): return {'id':device['id'],'name':device['name'],'available':device.get('peer') is not None and not device['peer'].closed,'paired':device.get('controller') is not None}
def token_entry(value,role,device_id=None):
    entry=TOKENS.get(value)
    return entry if entry and entry['role']==role and (device_id is None or entry['device']==device_id) else None

def revoke(device):
    for role in ('peer','controller'):
        if device.get(role): device[role].closed=True
    for key,entry in list(TOKENS.items()):
        if entry['device']==device['id']: TOKENS.pop(key,None)
    for key,entry in list(PAIRS.items()):
        if entry['device']==device['id']: PAIRS.pop(key,None)

def http(handler):
    try:
        path=handler.path
        with LOCK:
            if path.startswith('/api/connect/artwork/') and handler.command=='GET':
                tid=path.rsplit('/',1)[-1]
                entry=ARTWORKS.get(tid)
                if not entry:
                    handler.send_response(404);handler.end_headers();return
                data,ctype=entry
                handler.send_response(200)
                handler.send_header('Content-Type',ctype)
                handler.send_header('Content-Length',str(len(data)))
                handler.send_header('Cache-Control','public, max-age=3600')
                handler.end_headers()
                handler.wfile.write(data)
                return
            if path.startswith('/api/connect/artwork/') and handler.command=='POST':
                if not native(handler):return reply(handler,403,{})
                tid=path.rsplit('/',1)[-1]
                if not re.fullmatch(r'[a-zA-Z0-9_-]{16,64}',tid):return reply(handler,422,{})
                size=int(handler.headers.get('Content-Length','0'))
                if not 0<size<=5_000_000:return reply(handler,422,{})
                ctype=handler.headers.get('Content-Type','image/jpeg')
                raw=handler.rfile.read(size)
                if len(ARTWORKS)>=16:ARTWORKS.pop(next(iter(ARTWORKS)))
                ARTWORKS[tid]=(raw,ctype)
                return reply(handler,200,{'ok':True})
            if path=='/api/connect/devices' and handler.command=='GET':
                if not (native(handler) or browser(handler)): return reply(handler,403,{})
                return reply(handler,200,{'devices':[public(d) for d in DEVICES.values() if d.get('peer') and not d['peer'].closed]})
            if path=='/api/connect/register' and handler.command=='POST':
                if not browser(handler): return reply(handler,403,{})
                data=body(handler);identifier=data.get('id')
                if not isinstance(identifier,str) or not re.fullmatch(r'[a-f0-9]{32}',identifier):return reply(handler,422,{})
                for stale_id,stale in list(DEVICES.items()):
                    if not stale.get('peer') and time.monotonic()-stale.get('created',time.monotonic())>20:
                        revoke(stale);DEVICES.pop(stale_id,None)
                if len(DEVICES)>=8 and identifier not in DEVICES:return reply(handler,429,{})
                old=DEVICES.get(identifier)
                if old: revoke(old)
                key=secrets.token_hex(32)
                device={'id':identifier,'created':time.monotonic(),'name':str(data.get('name') or 'Aleztesia · Pantalla')[:60],'peer':None,'controller':None,'receiverToken':key}
                DEVICES[identifier]=device;TOKENS[key]={'role':'receiver','device':identifier}
                return reply(handler,200,{'device':public(device),'token':key})
            if path=='/api/connect/pair' and handler.command=='POST':
                if not native(handler):return reply(handler,403,{})
                data=body(handler);identifier=data.get('deviceId');device=DEVICES.get(identifier) if isinstance(identifier,str) else None;now=time.monotonic()
                ATTEMPTS[:]=[t for t in ATTEMPTS if now-t<60]
                if len(ATTEMPTS)>=30:return reply(handler,429,{'message':'Demasiados intentos. Espera un minuto antes de conectar.'})
                if not device or not device.get('peer') or device['peer'].closed:return reply(handler,404,{'message':'La salida cambió o se cerró. Actualiza la lista de dispositivos.'})
                for old_token,entry in list(TOKENS.items()):
                    if entry['device']==identifier and entry['role']=='controller':TOKENS.pop(old_token,None)
                for old_request,pair in list(PAIRS.items()):
                    if pair['device']==identifier:PAIRS.pop(old_request,None)
                old_ctrl=device.get('controller')
                if old_ctrl:
                    old_ctrl.push({'type':'disconnected'})
                    old_ctrl.closed=True
                    device['controller']=None
                ATTEMPTS.append(now)
                request_id=secrets.token_hex(16);poll=secrets.token_hex(32)
                key=secrets.token_hex(32)
                TOKENS[key]={'role':'controller','device':device['id']}
                for k,v in list(PAIRS.items()):
                    if now-v['created']>60:PAIRS.pop(k)
                name=str(data.get('name','Celular'))[:80]
                PAIRS[request_id]={'created':now,'device':device['id'],'poll':poll,'decision':True,'token':key,'name':name}
                device['peer'].push({'type':'paired','requestId':request_id,'name':name})
                return reply(handler,200,{'requestId':request_id,'pollToken':poll,'token':key,'device':public(device)})
            if path.startswith('/api/connect/pair/') and handler.command=='GET':
                if not native(handler):return reply(handler,403,{})
                p=PAIRS.get(path.rsplit('/',1)[-1])
                if not p or not secrets.compare_digest(p['poll'],bearer(handler)):return reply(handler,401,{})
                if time.monotonic()-p['created']>35:return reply(handler,410,{})
                if p['decision'] is None:return reply(handler,202,{'pending':True})
                if not p['decision']:return reply(handler,403,{'message':'Conexión rechazada en la pantalla.'})
                return reply(handler,200,{'token':p['token'],'device':public(DEVICES[p['device']])})
            if path=='/api/connect/leave' and handler.command=='POST':
                entry=token_entry(bearer(handler),'controller')
                if not native(handler) or not entry:return reply(handler,401,{})
                device=DEVICES.get(entry['device']);peer=device.get('controller') if device else None
                if peer:peer.closed=True
                if device and device.get('peer'):device['peer'].push({'type':'disconnected'})
                TOKENS.pop(bearer(handler),None)
                return reply(handler,200,{'ok':True})
        return reply(handler,404,{})
    except (ValueError,TypeError):return reply(handler,422,{})

class Peer:
    def __init__(self,role,device):self.role=role;self.device=device;self.out=queue.Queue(512);self.closed=False
    def push(self,data):
        if self.closed:return
        try:self.out.put_nowait(data)
        except queue.Full:
            try:self.out.get_nowait()
            except queue.Empty:pass
            try:self.out.put_nowait(data)
            except queue.Full:pass

def message(peer,data):
    with LOCK:
        device=DEVICES.get(peer.device)
        if not device or device.get('peer' if peer.role=='receiver' else 'controller') is not peer:return
        if not isinstance(data,dict):return
        if peer.role=='receiver':
            if data.get('type')=='rename':
                name=str(data.get('name','')).strip()[:60]
                if name:device['name']=name
                return
            if data.get('type')=='pairDecision':
                p=PAIRS.get(data.get('requestId'))
                if not p or p['device']!=peer.device or p['decision'] is not None or time.monotonic()-p['created']>35:return
                approved=data.get('approved') is True;p['decision']=approved
                if approved:
                    if device.get('controller'):p['decision']=False;return
                    key=secrets.token_hex(32);p['token']=key;TOKENS[key]={'role':'controller','device':peer.device}
            elif data.get('type') in ('state','response','intent'):
                controller=device.get('controller')
                if controller:controller.push(data)
        elif data.get('type')=='command' and device.get('controller') is peer:
            if data.get('method') not in ('prepare','commit','abort','play','pause','seek','volume','stop','lyrics','queue'):return
            if not isinstance(data.get('id'),str) or len(data['id'])>100:return
            receiver=device.get('peer')
            if receiver:receiver.push(data)

def websocket(handler):
    match=re.fullmatch('/ws/connect/(receiver|controller)/([a-f0-9]{32})',handler.path)
    if not match:return reply(handler,404,{})
    role,identifier=match.groups()
    if not (browser(handler) if role=='receiver' else native(handler)):return reply(handler,403,{})
    protocols=[p.strip() for p in handler.headers.get('Sec-WebSocket-Protocol','').split(',')]
    key=next((p.removeprefix('aleztesia.token.') for p in protocols if p.startswith('aleztesia.token.')),None) if role=='receiver' else bearer(handler)
    with LOCK:
        entry=token_entry(key,role,identifier);device=DEVICES.get(identifier)
        if not entry or not device:return reply(handler,401,{})
        slot='peer' if role=='receiver' else 'controller'
        if device.get(slot):
            if role=='controller':
                old=device[slot]
                old.closed=True
                device[slot]=None
            else:
                return reply(handler,409,{})
        peer=Peer(role,identifier);device[slot]=peer
    from websockets.server import ServerProtocol
    from websockets.frames import Opcode
    protocol=ServerProtocol(subprotocols=[PROTOCOL] if role=='receiver' else None,max_size=2_000_000)
    head=f'GET {handler.path} HTTP/1.1\r\n'+''.join(f'{k}: {v}\r\n' for k,v in handler.headers.items())+'\r\n'
    try:
        protocol.receive_data(head.encode());events=protocol.events_received()
        if not events:return
        response=protocol.accept(events[0]);protocol.send_response(response)
        for raw in protocol.data_to_send():handler.connection.sendall(raw)
        handler.close_connection=True
        if response.status_code!=101:return
        handler.connection.settimeout(.1)
        peer.push({'type':'welcome','device':public(device)})
        fragments=bytearray();fragment_opcode=None
        while not peer.closed:
            try:
                raw=handler.connection.recv(65536)
                if not raw:break
                protocol.receive_data(raw)
                for event in protocol.events_received():
                    opcode=getattr(event,'opcode',None)
                    if opcode in (Opcode.TEXT,Opcode.BINARY):
                        fragments=bytearray();fragment_opcode=opcode
                    if opcode in (Opcode.TEXT,Opcode.BINARY,Opcode.CONT):
                        fragments.extend(event.data)
                        if len(fragments)>2_000_000:peer.closed=True;break
                        if event.fin:
                            if fragment_opcode==Opcode.TEXT:
                                try:message(peer,json.loads(fragments))
                                except (ValueError,TypeError,UnicodeError):pass
                            fragments=bytearray();fragment_opcode=None
            except socket.timeout:pass
            try:
                while not peer.out.empty():protocol.send_text(json.dumps(peer.out.get_nowait()).encode())
                for raw in protocol.data_to_send():
                    if raw:handler.connection.sendall(raw)
                    else:peer.closed=True
            except Exception:peer.closed=True;break
    except Exception:pass
    finally:
        with LOCK:
            current=DEVICES.get(identifier)
            if current is device and device.get(slot) is peer:
                device[slot]=None
                if role=='receiver':
                    other=device.get('controller')
                    if other:other.push({'type':'disconnected'});other.closed=True
                    revoke(device)
                    DEVICES.pop(identifier,None)
                else:
                    if device.get('peer'):device['peer'].push({'type':'disconnected'})
                    TOKENS.pop(key,None)
                    for request_id,pair in list(PAIRS.items()):
                        if pair.get('token')==key:PAIRS.pop(request_id,None)
