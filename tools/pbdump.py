import sys, struct
def varint(b,i):
    r=0;s=0
    while True:
        c=b[i];i+=1;r|=(c&0x7f)<<s;s+=7
        if c<0x80:return r,i
def parse(b):
    """Return list of (field, wiretype, value) or None if not valid protobuf."""
    i=0;out=[]
    try:
        while i<len(b):
            k,i=varint(b,i);f=k>>3;w=k&7
            if f==0:return None
            if w==0:v,i=varint(b,i)
            elif w==1:v=b[i:i+8];i+=8
            elif w==2:
                l,i=varint(b,i);v=b[i:i+l];i+=l
                if i>len(b):return None
            elif w==5:v=b[i:i+4];i+=4
            else:return None
            out.append((f,w,v))
        return out if i==len(b) else None
    except IndexError:return None
def show(b,ind=0,maxd=8,maxitems=60):
    p=parse(b)
    if p is None: print(' '*ind+'<bytes %d>'%len(b), b[:32].hex()); return
    for n,(f,w,v) in enumerate(p):
        if n>=maxitems: print(' '*ind+'... %d more'%(len(p)-n));break
        pre=' '*ind+f'{f}:'
        if w==0: print(pre,v)
        elif w==5: print(pre,'f32',struct.unpack('<f',v)[0],'u32',struct.unpack('<I',v)[0])
        elif w==1: print(pre,'f64',struct.unpack('<d',v)[0])
        else:
            try:
                s=v.decode('utf8')
                if s.isprintable() and len(s)>0: print(pre,repr(s));continue
            except:pass
            sub=parse(v) if len(v)>0 and ind<maxd*2 else None
            if sub is not None and len(v)>1:
                print(pre,'{ (%d bytes)'%len(v)); show(v,ind+2,maxd,maxitems); print(' '*ind+'}')
            else: print(pre,'<bytes %d>'%len(v), v[:48].hex())
if __name__=='__main__':
    show(open(sys.argv[1],'rb').read(), maxitems=int(sys.argv[2]) if len(sys.argv)>2 else 60)
