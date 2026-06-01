"""Phase 1 page MD 후처리: 코드블록 래핑 제거 + 불필요한 frontmatter 필드 정리.

사용법:
    python3 scripts/cleanup_phase1.py <doc_id>
"""
import re, sys, pathlib

def cleanup(doc_id: str) -> None:
    tick3 = chr(96) * 3
    d = pathlib.Path("extracted") / doc_id
    if not d.exists():
        print(f"경로 없음: {d}")
        sys.exit(1)

    fixed = 0
    for f in sorted(d.glob("page_*.md")):
        txt = f.read_text(encoding="utf-8")
        original = txt
        lines = txt.split("\n")

        # 코드블록 래핑 제거
        if lines[0].strip() in (tick3, tick3 + "markdown"):
            close = next(
                (i for i, l in enumerate(lines) if i > 0 and l.strip() == tick3),
                None,
            )
            txt = "\n".join(lines[1:close]) if close else txt

        # 검증 섹션 제거
        txt = re.split(r"\n---\s*\n\s*##\s*(검증|検証)", txt)[0].rstrip()

        # doc_id 필드 제거
        txt = re.sub(r"\ndoc_id:[^\n]*", "", txt)

        txt = txt + "\n"
        if txt != original:
            f.write_text(txt, encoding="utf-8")
            fixed += 1

    print(f"후처리 완료: {fixed}개 파일 수정 ({d})")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("사용법: python3 scripts/cleanup_phase1.py <doc_id>")
        sys.exit(1)
    cleanup(sys.argv[1])
