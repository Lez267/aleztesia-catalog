"""Online catalogue service. Anonymous public catalogue, no phone/account credentials.
Only catalogue identities are accepted; this is never an arbitrary URL proxy.
"""
import json,re,secrets,time,threading
from urllib.parse import urlsplit,parse_qs
from concurrent.futures import Future
import connect

LOCK=threading.RLock()
STREAMS={}
VIDEO=re.compile(r'[A-Za-z0-9_-]{11}')
BROWSE=re.compile(r'[A-Za-z0-9_-]{8,180}')
CATALOG_LOCK=threading.RLock()
_CLIENT=None
CACHE={}
IN_FLIGHT={}

def cached_result(key,ttl,fetch):
    now=time.monotonic()
    with LOCK:
        cached=CACHE.get(key)
        if cached and cached[0]>now:return cached[1]
        future=IN_FLIGHT.get(key)
        owner=future is None
        if owner:future=IN_FLIGHT[key]=Future()
    if not owner:return future.result(timeout=40)
    try:
        result=fetch()
        with LOCK:
            for old,value in list(CACHE.items()):
                if value[0]<=now:CACHE.pop(old,None)
            while len(CACHE)>=128:CACHE.pop(next(iter(CACHE)))
            CACHE[key]=(time.monotonic()+(ttl if result is not None else 15),result)
        future.set_result(result)
        return result
    except BaseException as error:
        future.set_exception(error)
        raise
    finally:
        with LOCK:IN_FLIGHT.pop(key,None)


def authorized(handler):return getattr(handler,'public_catalog',False) or connect.native(handler) or connect.browser(handler)

def client():
    global _CLIENT
    if _CLIENT is None:
        from ytmusicapi import YTMusic
        _CLIENT=YTMusic(language='en',location='MX')
    return _CLIENT

def catalogue_call(method,*args,**kwargs):
    # One persistent session, guarded because requests.Session is not thread-safe.
    with CATALOG_LOCK:return getattr(client(),method)(*args,**kwargs)

def normalize(item):
    if not isinstance(item,dict):return None
    kind=item.get('resultType')
    identifier=item.get('videoId')
    if identifier:kind='song'
    else:
        identifier=item.get('browseId') or item.get('playlistId')
        if not kind:
            kind='album' if str(identifier).startswith('MPRE') else 'artist' if str(identifier).startswith('UC') else 'playlist'
    if kind not in ('song','video','album','artist','playlist') or not isinstance(identifier,str):return None
    title=item.get('title') or item.get('artist') or item.get('name')
    if not isinstance(title,str):return None
    artists=item.get('artists') or item.get('author') or []
    if isinstance(artists,str):artist=artists
    elif isinstance(artists,dict):artist=artists.get('name','')
    else:artist=', '.join(str(a.get('name','')) for a in artists if isinstance(a,dict))
    if not artist and kind!='artist' and isinstance(item.get('artist'),str):artist=item['artist']
    thumbs=[t for t in item.get('thumbnails') or [] if isinstance(t,dict) and str(t.get('url','')).startswith('https://')]
    thumb=max(thumbs,key=lambda t:(t.get('width') or 0)*(t.get('height') or 0),default={}).get('url')
    duration=item.get('duration_seconds')
    if not isinstance(duration,(int,float)):
        try:
            duration=0
            for part in str(item.get('duration','0')).split(':'):duration=duration*60+int(part)
        except ValueError:duration=0
    return {'id':identifier,'kind':kind,'title':title[:500],'artist':artist[:500],
        'art':thumb,'durationMs':max(0,duration)*1000,'album':item.get('album')}

def items(values):return [v for row in values if (v:=normalize(row))]

class QuietLog:
    def debug(self,*args):pass
    def warning(self,*args):print('[yt-dlp-warn]',*args)
    def error(self,*args):print('[yt-dlp-error]',*args)

def safe_media_url(url):
    parsed=urlsplit(url)
    return parsed.scheme=='https' and not parsed.username and not parsed.password and parsed.port in (None,443) and (parsed.hostname or '').endswith('.googlevideo.com')

def resolve(identifier):
    if not VIDEO.fullmatch(identifier):raise ValueError('Canción inválida.')
    return cached_result(('audio',identifier),1800,lambda:_resolve_uncached(identifier))

def _resolve_uncached(identifier):
    if not VIDEO.fullmatch(identifier):raise ValueError('Canción inválida.')
    from yt_dlp import YoutubeDL
    options={'format':'bestaudio/best','noplaylist':True,'skip_download':True,'quiet':False,
        'logger':QuietLog(),'socket_timeout':15,'retries':2,'extractor_retries':2,'cachedir':False,
        'js_runtimes':{'node':{}},
        'remote_components':{'ejs:github'}}
    import os, base64
    cookies_data = os.environ.get('YOUTUBE_COOKIES', '').strip()
    if cookies_data:
        try:
            if not cookies_data.startswith('# Netscape'):
                decoded = base64.b64decode(cookies_data).decode('utf-8', errors='ignore')
                if 'youtube.com' in decoded or '# Netscape' in decoded: cookies_data = decoded
        except Exception: pass
        cookie_path = '/tmp/youtube_cookies.txt'
        try:
            with open(cookie_path, 'w') as f: f.write(cookies_data)
            options['cookiefile'] = cookie_path
        except Exception: pass
    try:
        with YoutubeDL(options) as downloader:
            result = downloader.extract_info('https://www.youtube.com/watch?v=' + identifier, download=False)
    except Exception as e:
        if 'Requested format is not available' in str(e):
            fallback_opts = dict(options)
            fallback_opts.pop('format', None)
            with YoutubeDL(fallback_opts) as downloader:
                info = downloader.extract_info('https://www.youtube.com/watch?v=' + identifier, download=False)
                audio_formats = [f for f in info.get('formats', []) if f.get('url') and (f.get('acodec') != 'none' or f.get('vcodec') == 'none')]
                if not audio_formats:
                    audio_formats = [f for f in info.get('formats', []) if f.get('url')]
                if not audio_formats:
                    raise
                best = max(audio_formats, key=lambda f: (f.get('abr') or 0, f.get('tbr') or 0))
                result = {**info, 'url': best['url']}
        else:
            raise
    url=result.get('url','')
    if not safe_media_url(url):raise ValueError('El proveedor no entregó una fuente de audio compatible.')
    token=secrets.token_urlsafe(32)
    with LOCK:
        for key,value in list(STREAMS.items()):
            if time.monotonic()>value['expires']:
                STREAMS.pop(key,None);CACHE.pop(('audio',value.get('trackId')),None)
        while len(STREAMS)>=32:
            removed=STREAMS.pop(next(iter(STREAMS)));CACHE.pop(('audio',removed.get('trackId')),None)
        STREAMS[token]={'trackId':identifier,'url':url,'headers':result.get('http_headers',{}),'expires':time.monotonic()+3600}
    return {'url':'/api/music/stream/'+token,'durationMs':int((result.get('duration') or 0)*1000)}

def stream(handler,token):
    if not authorized(handler):return connect.reply(handler,403,{})
    with LOCK:entry=STREAMS.get(token)
    if not entry or entry['expires']<time.monotonic():return connect.reply(handler,410,{'message':'El enlace caducó. Vuelve a elegir la canción.'})
    import requests
    headers={k:v for k,v in entry['headers'].items() if k.lower() in ('user-agent','accept','accept-language')}
    headers['Accept-Encoding']='identity'
    byte_range=handler.headers.get('Range')
    if byte_range:
        if not re.fullmatch(r'bytes=(?:\d+-\d*|-\d+)',byte_range):return connect.reply(handler,416,{})
        headers['Range']=byte_range
    url=entry['url']
    for _ in range(4):
        if not safe_media_url(url):return connect.reply(handler,502,{'message':'Fuente de audio no permitida.'})
        response=requests.get(url,headers=headers,stream=True,timeout=(10,20),allow_redirects=False)
        if response.status_code in (301,302,303,307,308):
            url=response.headers.get('Location','');response.close();continue
        break
    else:return connect.reply(handler,502,{'message':'La fuente redirigió demasiadas veces.'})
    with response:
        if response.status_code not in (200,206,416):return connect.reply(handler,502,{'message':'El proveedor rechazó la reproducción. Prueba otra canción.'})
        handler.stream_started=True
        handler.send_response(response.status_code)
        for key in ('Content-Type','Content-Length','Content-Range','Accept-Ranges'):
            if key in response.headers:handler.send_header(key,response.headers[key])
        handler.send_header('Cache-Control','no-store');handler.end_headers()
        try:
            for chunk in response.iter_content(65536):
                if chunk:handler.wfile.write(chunk)
        except (BrokenPipeError,ConnectionResetError):pass


ART_HOSTS={'lh3.googleusercontent.com','yt3.googleusercontent.com','yt3.ggpht.com','i.ytimg.com'}
def safe_art_url(url):
    try:
        p=urlsplit(url)
        return len(url)<=4000 and p.scheme=='https' and p.hostname in ART_HOSTS and not p.username and not p.password and p.port in (None,443)
    except (ValueError,TypeError):return False

def blurred_artwork(url):
    if not safe_art_url(url):raise ValueError('Portada no permitida.')
    return cached_result(('backdrop',url),1800,lambda:_blurred_artwork_uncached(url))

def _blurred_artwork_uncached(url):
    import requests,io
    from PIL import Image,ImageFilter,ImageOps
    for _ in range(4):
        if not safe_art_url(url):raise ValueError('Portada no permitida.')
        with requests.get(url,timeout=(5,8),stream=True,allow_redirects=False) as response:
            if response.status_code in (301,302,303,307,308):
                from urllib.parse import urljoin
                url=urljoin(url,response.headers.get('Location',''));continue
            response.raise_for_status();body=bytearray()
            for chunk in response.iter_content(65536):
                body.extend(chunk)
                if len(body)>4_000_000:raise ValueError('Portada demasiado grande.')
            with Image.open(io.BytesIO(body)) as image:
                if image.width*image.height>16_000_000:raise ValueError('Portada demasiado grande.')
                small=ImageOps.fit(image.convert('RGB'),(64,64)).filter(ImageFilter.GaussianBlur(7))
                output=io.BytesIO();small.resize((256,256),Image.Resampling.BILINEAR).save(output,format='JPEG',quality=88)
                return output.getvalue()
    raise ValueError('No se pudo obtener la portada.')

LYRIC_HOSTS={'lrc.red','lyrics-api.binimum.org','lyrics-api.boidu.dev','unison.boidu.dev','lyricsplus.prjktla.my.id','lyricsplus.binimum.org','lrclib.net'}
def lyric_document(url):
    if not isinstance(url,str) or len(url)>4000:raise ValueError('Documento de letras inválido.')
    return cached_result(('lyric',url),600,lambda:_lyric_document_uncached(url))

def _lyric_document_uncached(url):
    if not isinstance(url,str) or len(url)>4000:raise ValueError('Documento de letras inválido.')
    import requests
    for _ in range(4):
        parsed=urlsplit(url)
        if parsed.scheme!='https' or parsed.hostname not in LYRIC_HOSTS or parsed.username or parsed.password or parsed.port not in (None,443):raise ValueError('Proveedor de letras no permitido.')
        with requests.get(url,timeout=(5,10),stream=True,allow_redirects=False) as response:
            if response.status_code in (301,302,303,307,308):
                from urllib.parse import urljoin
                url=urljoin(url,response.headers.get('Location',''));continue
            if response.status_code==404:return None
            response.raise_for_status();body=bytearray()
            for chunk in response.iter_content(65536):
                body.extend(chunk)
                if len(body)>2_000_000:raise ValueError('El documento de letras es demasiado grande.')
            return body.decode('utf-8-sig')
    return None

def http(handler):
    path=urlsplit(handler.path)
    if path.path.startswith('/api/music/stream/'):
        try:return stream(handler,path.path.rsplit('/',1)[-1])
        except (BrokenPipeError,ConnectionResetError):return
        except Exception:
            if not getattr(handler,'stream_started',False):return connect.reply(handler,502,{'message':'No se pudo abrir el audio del proveedor.'})
            return
    if not authorized(handler):return connect.reply(handler,403,{})
    try:
        query=parse_qs(path.query)
        action=path.path.removeprefix('/api/music/')
        if handler.command=='GET' and action=='backdrop':
            data=blurred_artwork(query.get('url',[''])[0]);handler.send_response(200)
            handler.send_header('Content-Type','image/jpeg');handler.send_header('Content-Length',str(len(data)))
            handler.send_header('Cache-Control','public, max-age=1800');handler.end_headers();handler.wfile.write(data);return
        elif handler.command=='GET' and action=='home':
            result={'shelves':[{'title':s['title'],'items':items(s.get('contents',[]))} for s in cached_result(('home',),300,lambda:catalogue_call('get_home',limit=5))]}
        elif handler.command=='GET' and action=='search':
            term=query.get('q',[''])[0].strip();kind=query.get('type',['songs'])[0]
            if not 1<=len(term)<=160 or kind not in ('songs','albums','artists','playlists'):raise ValueError('Búsqueda inválida.')
            result={'items':items(cached_result(('search',term,kind),120,lambda:catalogue_call('search',term,filter=kind,limit=30)))}
        elif handler.command=='GET' and action in ('album','playlist','artist'):
            identifier=query.get('id',[''])[0]
            if not BROWSE.fullmatch(identifier):raise ValueError('Identificador inválido.')
            if action=='album':
                album=cached_result(('album',identifier),300,lambda:catalogue_call('get_album',identifier));tracks=[{**t,'artists':t.get('artists') or album.get('artists',[]),'thumbnails':t.get('thumbnails') or album.get('thumbnails',[]),'album':{'id':identifier,'name':album.get('title','')}} for t in album.get('tracks',[])];result={'title':album.get('title','Álbum'),'items':items(tracks)}
            elif action=='playlist':
                playlist=cached_result(('playlist',identifier),300,lambda:catalogue_call('get_playlist',identifier.removeprefix('VL'),limit=100));result={'title':playlist.get('title','Playlist'),'items':items(playlist.get('tracks',[]))}
            else:
                artist=cached_result(('artist',identifier),300,lambda:catalogue_call('get_artist',identifier));result={'title':artist.get('name','Artista'),'items':items(artist.get('songs',{}).get('results',[])),'shelves':[{'title':key,'items':items(artist.get(key,{}).get('results',[]))} for key in ('albums','singles')]}
        elif handler.command=='POST' and action=='lyrics-document':result={'body':lyric_document(connect.body(handler).get('url',''))}
        elif handler.command=='POST' and action=='resolve':result=resolve(connect.body(handler).get('id',''))
        elif handler.command=='GET' and action=='lyrics':
            title=query.get('title',[''])[0][:300];artist=query.get('artist',[''])[0][:300]
            if not title:raise ValueError('Falta la canción.')
            import requests
            response=requests.get('https://lrclib.net/api/search',params={'track_name':title,'artist_name':artist},timeout=15)
            response.raise_for_status();matches=response.json()
            target=float(query.get('duration',['0'])[0])
            matches=sorted((m for m in matches if m.get('syncedLyrics')),key=lambda m:abs(float(m.get('duration') or 0)-target))
            result={'lrc':matches[0]['syncedLyrics'] if matches else '', 'source':'LRCLIB'}
        else:return connect.reply(handler,404,{})
        return connect.reply(handler,200,result)
    except (BrokenPipeError,ConnectionResetError):return
    except ValueError as error:return connect.reply(handler,422,{'message':str(error)})
    except ImportError:return connect.reply(handler,503,{'message':'El servicio de música no está configurado.'})
    except Exception as error:
        import traceback; traceback.print_exc()
        return connect.reply(handler,502,{'message':f'No se pudo consultar el proveedor ({error}).'})
