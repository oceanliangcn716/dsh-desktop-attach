#!/usr/bin/env python3
"""Generate a new generic phone icon; no screenshots or vendor logos are used."""
import json
from pathlib import Path
import struct
import zlib
ROOT = Path(__file__).resolve().parents[1]
PROJECT = json.loads((ROOT/'project.json').read_text())
OUT = ROOT/('web' if PROJECT['slug']=='codex-mobile' else 'home-icons')
def png(size):
    rows=[]
    for y in range(size):
        row=bytearray()
        for x in range(size):
            a=x/size; b=y/size
            color=(36,87,166,255)
            if .30<=a<=.70 and .14<=b<=.86: color=(245,248,255,255)
            if .35<=a<=.65 and .24<=b<=.74: color=(36,87,166,255)
            if (a-.5)**2+(b-.80)**2 < .025**2: color=(36,87,166,255)
            row.extend(color)
        rows.append(b'\x00'+row)
    def chunk(kind,data):
        return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!2I5B',size,size,8,6,0,0,0))+chunk(b'IDAT',zlib.compress(b''.join(rows),9))+chunk(b'IEND',b'')
def main():
    OUT.mkdir(exist_ok=True)
    for size in (180,192,512): (OUT/('icon-'+str(size)+'.png')).write_bytes(png(size))
    (OUT/'apple-touch-icon.png').write_bytes((OUT/'icon-180.png').read_bytes())
    if PROJECT['slug']=='dsh-desktop-attach':
        manifest={'name':'DSH Mobile','short_name':'DSH Mobile','start_url':'/','display':'standalone','icons':[{'src':'/dsh-mobile-icon-'+str(s)+'.png','sizes':str(s)+'x'+str(s),'type':'image/png'} for s in (192,512)]}
        (OUT/'manifest.webmanifest').write_text(json.dumps(manifest)+'\n')
if __name__=='__main__': main()
