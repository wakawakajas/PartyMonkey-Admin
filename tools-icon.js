// Renders the same mark as icon.svg straight to PNG, so the two cannot drift.
// Pure geometry — no canvas, no font, nothing to install.
const fs=require('fs'), zlib=require('zlib');

const LIME=[0xCC,0xF3,0x44], DARK=[0x17,0x1A,0x0B];
const U=512;                       // the svg's coordinate space

// glyph, in svg units: stem + bowl, minus the counter
function inGlyph(x,y){
  const dx=x-279, dy=y-192, d2=dx*dx+dy*dy;
  const outer=(x>=157&&x<=279&&y>=116&&y<=268)
           || (x>=279&&d2<=76*76)
           || (x>=157&&x<=209&&y>=268&&y<=396);
  if(!outer) return false;
  const counter=(x>=209&&x<=279&&y>=168&&y<=216)||(x>=279&&d2<=24*24);
  return !counter;
}
// the drafting grid the app sits on, at 10% ink
function gridInk(x,y){
  const near=v=>{ const m=v%64; return Math.min(m,64-m)<=1; };
  return (near(x)||near(y))?0.10:0;
}

function render(size){
  const S=4, k=U/size;                     // 4x4 supersampling
  const row=size*3+1, raw=Buffer.alloc(row*size);
  for(let py=0;py<size;py++){
    raw[py*row]=0;                          // filter: none
    for(let px=0;px<size;px++){
      let g=0, ink=0;
      for(let sy=0;sy<S;sy++) for(let sx=0;sx<S;sx++){
        const x=(px+(sx+0.5)/S)*k, y=(py+(sy+0.5)/S)*k;
        if(inGlyph(x,y)) g++; else ink+=gridInk(x,y);
      }
      const gf=g/(S*S), inkf=ink/(S*S);
      const o=py*row+1+px*3;
      for(let c=0;c<3;c++){
        const bg=LIME[c]*(1-inkf)+DARK[c]*inkf;   // grid over lime
        raw[o+c]=Math.round(bg*(1-gf)+DARK[c]*gf); // glyph over that
      }
    }
  }
  const chunk=(type,data)=>{
    const len=Buffer.alloc(4); len.writeUInt32BE(data.length);
    const body=Buffer.concat([Buffer.from(type,'ascii'),data]);
    const crc=Buffer.alloc(4); crc.writeUInt32BE(crc32(body));
    return Buffer.concat([len,body,crc]);
  };
  const ihdr=Buffer.alloc(13);
  ihdr.writeUInt32BE(size,0); ihdr.writeUInt32BE(size,4);
  ihdr[8]=8; ihdr[9]=2;                     // 8-bit truecolour RGB
  return Buffer.concat([
    Buffer.from([0x89,0x50,0x4E,0x47,0x0D,0x0A,0x1A,0x0A]),
    chunk('IHDR',ihdr),
    chunk('IDAT',zlib.deflateSync(raw,{level:9})),
    chunk('IEND',Buffer.alloc(0))
  ]);
}

const T=(()=>{const t=new Int32Array(256);
  for(let n=0;n<256;n++){let c=n;for(let k=0;k<8;k++)c=(c&1)?(0xEDB88320^(c>>>1)):(c>>>1);t[n]=c;}
  return t;})();
function crc32(buf){
  let c=0xFFFFFFFF;
  for(const b of buf) c=T[(c^b)&0xFF]^(c>>>8);
  return (c^0xFFFFFFFF)>>>0;
}

const dir='C:/Users/Admin/Desktop/Claude/print-template-builder/';
for(const s of [180,192,512]){
  const png=render(s);
  fs.writeFileSync(dir+'icon-'+s+'.png',png);
  console.log('icon-'+s+'.png',png.length+'b');
}
