# 앱 아이콘 생성: 돋보기 안 드론 + 오른쪽 위 DMD, 왼쪽 아래 GCS
#   python3 docs/make_icons.py            → app/src/main/res/mipmap-*, web/icon-*.png, docs/app-icon.svg 를 다시 만듦
#   python3 docs/make_icons.py --preview  → 미리보기만 (docs/icon-preview.png)
# 필요: pip install playwright pillow && playwright install chromium
import sys, io
from playwright.sync_api import sync_playwright
from PIL import Image
# 돋보기·드론은 원래 크기 그대로 두고, 빈 모서리에 글자만 넣음 (좌표는 모두 512 기준)
#   일반 아이콘: 원래 그림 그대로 + 오른쪽 위 DMD · 왼쪽 아래 GCS
#   적응형 앞면(108dp 캔버스): 원래 앱과 같은 크기(0.677배)의 돋보기 + 런처가 원으로 잘라도 보이는 자리에 작은 글자
LAY={"icon":dict(k=1.0,t=0.0,fs=56,dmd=(394,85),gcs=(112,462)),
     "fg":dict(k=0.677,t=81.7,fs=25,dmd=(340,149),gcs=(182,376))}
for a in sys.argv[1:]:
    if "=" in a:
        k,v=a.split("=");L,f=k.split(".");v=[float(x) for x in v.split(",")];LAY[L][f]=tuple(v) if len(v)>1 else v[0]
MAG='''<path d="M336 336 L420 420" stroke="#fff" stroke-width="56" stroke-linecap="round"/>
  <circle cx="230" cy="230" r="148" fill="#fff" stroke="#fff" stroke-width="30"/>
  <circle cx="230" cy="230" r="132" fill="none" stroke="#dbe7ff" stroke-width="4"/>
  <g transform="translate(230 240) scale(1.06)" fill="#6aa5ff">
    <g fill="none" stroke="#6aa5ff" stroke-linecap="round">
      <ellipse cx="-66" cy="-44" rx="46" ry="9" stroke-width="7"/><ellipse cx="66" cy="-44" rx="46" ry="9" stroke-width="7"/>
      <path d="M-30 2 L-36 42 M30 2 L36 42" stroke-width="10"/>
    </g>
    <path d="M-66 -58 l-6 12 h12z M66 -58 l-6 12 h12z"/><path d="M-74 -38 h16 l3 16 h-22z M58 -38 h16 l3 16 h-22z"/>
    <rect x="-84" y="-26" width="168" height="12" rx="6"/>
    <path d="M-42 -20 q0-16 16-16 h52 q16 0 16 16 l-8 22 q-2 6-8 6 h-52 q-6 0-8-6z"/>
    <rect x="-17" y="10" width="34" height="30" rx="9"/><circle cx="0" cy="25" r="9" fill="#fff"/>
  </g>'''
def comp(lay):
    """512 기준 그림: 돋보기(k배, t만큼 이동) + 글자 (dmd·gcs = 글자 가운데 x, 글자 밑줄 y)"""
    L=LAY[lay];fs=L["fs"]
    txt=lambda t,p:f'<text x="{p[0]:.1f}" y="{p[1]:.1f}" text-anchor="middle" font-family="DejaVu Sans, Arial, Helvetica, sans-serif" font-weight="700" font-size="{fs:.1f}" textLength="{fs*2.2:.1f}" lengthAdjust="spacingAndGlyphs" fill="#fff">{t}</text>'
    return f'<g transform="translate({L["t"]} {L["t"]}) scale({L["k"]})">{MAG}</g>'+txt("DMD",L["dmd"])+txt("GCS",L["gcs"])
BG='<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#3d86ff"/><stop offset="1" stop-color="#1650d6"/></linearGradient>'
def svg(size,kind):
    """kind: square(둥근 사각형 배경) / full(꽉 찬 사각형, 웹) / fg(적응형 앞면, 배경 없음) / bg(적응형 배경)"""
    back={"square":'<rect width="512" height="512" rx="112" fill="url(#bg)"/>',"full":'<rect width="512" height="512" fill="url(#bg)"/>',
          "fg":"","bg":'<rect width="512" height="512" fill="url(#bg)"/>'}[kind]
    body={"square":comp("icon"),"full":comp("icon"),"fg":comp("fg"),"bg":""}[kind]
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 512 512"><defs>{BG}</defs>{back}{body}</svg>'
def render(pg,s,size):
    pg.set_viewport_size({"width":size,"height":size})
    pg.set_content(f'<html><body style="margin:0;background:transparent">{s}</body></html>')
    return Image.open(io.BytesIO(pg.screenshot(omit_background=True,clip={"x":0,"y":0,"width":size,"height":size}))).convert("RGBA")

def install(pg,root):
    """앱 프로젝트에 크기별로 저장 (고해상도로 그린 뒤 줄여서 선명하게)"""
    big={k:render(pg,svg(1024,k),1024) for k in ("square","fg","bg","full")}
    sz=lambda k,n:big[k].resize((n,n),Image.LANCZOS)
    for d,n in {"mdpi":48,"hdpi":72,"xhdpi":96,"xxhdpi":144,"xxxhdpi":192}.items():
        base=f"{root}/app/src/main/res/mipmap-{d}/"
        sz("square",n).save(base+"ic_launcher.png");sz("square",n).save(base+"ic_launcher_round.png")   # 예전 앱처럼 둥근 사각형
        sz("fg",n*9//4).save(base+"ic_launcher_foreground.png");sz("bg",n*9//4).convert("RGB").save(base+"ic_launcher_background.png")
    for n in (192,512):sz("full",n).convert("RGB").save(f"{root}/web/icon-{n}.png")
    open(f"{root}/docs/app-icon.svg","w").write(svg(512,"square"))

if __name__=="__main__":
    import os
    root=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with sync_playwright() as p:
        pg=p.chromium.launch().new_page()
        if "--preview" in sys.argv:render(pg,svg(512,"square"),512).save(f"{root}/docs/icon-preview.png")
        else:install(pg,root);print("아이콘을 다시 만들었습니다")
