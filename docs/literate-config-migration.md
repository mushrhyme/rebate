# Literate Config 마이그레이션 — 룰 단일 진실 소스

**작성일**: 2026-06-17
**대상**: 개발자 — 이중 소스(고질병) 제거 설계·실행 계획
**관련**: [architecture.md](architecture.md) §7 의사결정 기록·§10 두 레이어, [phase4-dsl-readiness.md](phase4-dsl-readiness.md), [registry-driven-primitives.md](registry-driven-primitives.md)

---

## 1. 푸는 문제 — 이중 소스가 고질병이다

같은 사실(NET 수식·컬럼명·교차검증·출력설정·집계 전략)이 **두 곳**에 산다:

| 파일 | 표현 | 소유 | 코드가 직접 읽나 |
|---|---|---|---|
| `form_definitions/form_XX.md` | 자연어 산문 + 표 | 현업(개념) | ❌ |
| `config/form_types.json` | 실행 가능 구조 | 코드 | ✅ |

둘을 잇는 다리가 **sync-form-config 스킬 = 자연어→구조 LLM 추론**. 이게 깨지기 쉬운 방향이다. 산문→구조는 추론, 구조→산문은 렌더링. 시스템이 하필 *취약한 방향을 표준 실행 경로에* 두고 있었다.

파생 증상(모두 실측됨):
- **드리프트** — MD 수정 후 sync 누락 → JSON 구버전. diagnose 스킬 축4가 이걸 잡으려고 존재.
- **sync 사각** — `recovery_cell_map`·`product_aggregate`는 "MD에서 파싱하지 않는다"(sync SKILL.md). 즉 MD는 이미 *완전한 정본이 아니고*, 일부 필드는 JSON에만 수동 존재.
- **배포 유실 사고**(form_04, 커밋 f388561) — `--force-local` 배포 + stale 미러가 form_04.md의 NET·교차검증·출력설정 3섹션을 삭제. 정보가 두 파일에 중복돼 있어 *한쪽만 날아가도 파이프라인이 멈추지 않았다* → 조용한 손상. de3dd3b 미러 가드는 대증치료.

> **근본 원인은 "이중 소스" 한 줄.** [architecture.md](architecture.md) §7의 "룰 위치 = form 정의 MD + form_types.json (이중)" 결정이 만든 부채.

---

## 2. 결정 — Literate config (정본 = MD 안의 한 블록)

> **결정.** 각 `form_XX.md` 안에 **실행 가능한 `[config]` 정본 블록**(fenced JSON)을 둔다. 이 블록이 **유일한 진실 소스**다. `config/form_types.json`은 그 블록에서 **결정적으로 빌드되는 생성물**(빌드 캐시)이며 손으로 편집하지 않는다.

핵심 효과:
- **드리프트 불가** — 정본이 한 곳. sync의 산문→구조 추론이 표준 경로에서 사라진다(NL→구조 저작은 cold-start/update-form에서 *1회*만, 결과를 블록에 바로 적음).
- **사고 불가** — form_XX.md 유실 = 블록 유실 = **빌드 실패(시끄럽게)**. stale JSON을 조용히 배포하던 경로가 막힌다.
- **sync 사각 해소** — `recovery_cell_map` 같은 개발자 필드도 그냥 블록 안에 산다(파싱 대상이 아니라 *그 자체가 정본*). 현업은 블록 주변 산문을 읽고, 개발자는 블록을 관리.

### 왜 "MD 안의 블록"인가 (vs JSON 정본·MD 생성)

| 안 | 정본 | 트레이드오프 |
|---|---|---|
| **A. Literate config (채택)** | MD의 `[config]` 블록 | 한 파일에 산문+구조 공존. 현업의 "내가 읽는 파일" 철학 유지. 사고/드리프트 구조적 차단 |
| B. JSON 정본, MD 생성 | form_types.json | 코드 친화적이나 현업이 보는 MD가 생성물 → 시스템 철학("현업은 MD를 본다") 약화 |

CLAUDE.md상 현업은 어차피 MD를 **직접 편집하지 않는다**(Claude와 대화로 만든다). 소유는 개념적. → A가 철학을 가장 적게 흔든다.

---

## 3. 블록 형식

각 `form_XX.md` 끝에 단일 섹션:

```markdown
## [config] 실행 설정 (정본 · build_form_types.py가 읽음)

> 이 블록이 이 양식의 **유일한 실행 정본**이다. `config/form_types.json`은 여기서 빌드된 생성물.
> 위쪽 산문은 *근거·설명*이고, 실행 값은 이 블록이 결정한다. 둘이 어긋나면 이 블록이 이긴다.

​```json
{ ...form_XX 설정 객체 (현 form_types.json의 해당 양식 값과 동일)... }
​```
```

규칙:
- `## [config]` 섹션 + 그 안의 **첫 번째 ` ```json ` 펜스**가 정본. 파일당 정확히 하나.
- 블록 JSON은 form_id를 키로 감싸지 **않는다**(설정 객체 자체). form_id는 파일명(`form_04.md` → `form_04`)에서 도출.
- 직렬화: `ensure_ascii=False`, `indent=2`. 일본어 원문 그대로.

---

## 4. 빌드·가드 메커니즘

### 4.1 `scripts/build_form_types.py`

```
for form_definitions/form_XX.md:
    block = extract_config_block(md)        # [config] 섹션의 json 펜스
    obj   = json.loads(block)
    forms[stem] = obj                       # stem = "form_04"
validate(forms, form_types.schema.json)     # 기존 스키마 재사용
write config/form_types.json                # ensure_ascii=False, indent=2, 파일명 정렬
```

- 블록 없음/JSON 깨짐/스키마 불일치 → **즉시 비0 종료**(숨은 기본값 없음). 배포 유실이 조용한 손상 대신 시끄러운 실패가 되는 지점.
- 출력은 현 `config/form_types.json`과 **바이트 동일**(키 순서·들여쓰기·trailing newline 없음)하게 맞춘다 → 회귀·가드 무변동.

### 4.2 가드 테스트 (`tests/unit/`)

`build(form_XX.md 블록) == 현 config/form_types.json` 바이트 동치를 assert. 누군가 JSON을 손으로 고치거나, MD 블록을 고치고 rebuild를 빠뜨리면 **CI 적색**. 이게 "단일 소스"를 강제하는 실집행 장치.

### 4.3 blast radius (최소화 설계)

기존 readers(phase4_calc.py, schema/regression/contract/formula_impact 테스트, runtime_config_guard, 배포 미러)는 **계속 `config/form_types.json`을 읽는다 — 변경 없음**. 추가되는 것만:
- `scripts/build_form_types.py` (신규)
- 가드 테스트 1개 (신규)
- 각 form_XX.md의 `[config]` 블록 (추가 — 현 JSON에서 무손실 추출)
- sync-form-config / diagnose / deploy 문구 갱신 (P2)

---

## 5. 단계 (무변동 증명 경유)

| Phase | 산출물 | 위험 | 무변동 증명 |
|---|---|---|---|
| **P0 (이 문서 + §7 기록)** | 설계 합의 | — | — |
| **P1. 메커니즘 + 무손실 이관** | `build_form_types.py` + 가드 테스트 + 각 form_XX.md `[config]` 블록(현 JSON에서 추출). phase4는 여전히 JSON을 읽음 | 낮음 | `build==JSON` 바이트 동일 + 전체 회귀 그린 |
| **P2. 정본 전환** | 빌드를 sync-form-config의 동작으로 교체(산문→JSON 추론 폐기, 블록 추출+rebuild로). diagnose 축4·deploy 미러·MANUAL/CLAUDE 문구를 "블록=정본"으로 갱신 | 중(운영 문서·스킬) | 가드 테스트 유지, 회귀 그린 |
| **P3. 산문 규율** | 실행 specifics를 산문에서 블록으로 이관, 산문은 *근거(why)*만. cold-start/update-form이 블록을 직접 작성하도록 | 낮음 | 가드 테스트 유지 |

**P1 먼저인 이유:** 자기완결·가역·즉시 안전망(가드) 제공. 현 JSON에서 블록을 추출하므로 **정의상 무손실**. 통과하면 P2에서 정본을 옮긴다.

### P3 완료 (2026-06-18) — 정본-only 단일화

산문→구조 LLM 추론(`_claude_parse_md_to_entry`)·auto 블록 기록(`_write_auto_block`·prose-sha)·blockless 폴백을 **표준 경로에서 영구 제거**했다. 결과:

- **sync = 블록 빌드만.** `[config]` 블록이 유일한 정본. 블록이 없으면 sync는 *시끄럽게 실패*("정본 블록 없음 — cold-start/규칙 반영으로 생성")한다. 산문에서 조용히 자동생성하던 무음 no-op·블록 타입 혼재(auto/blockless)가 구조적으로 불가능해졌다.
- **신규 양식의 첫 블록 = cold-start/create가 부착.** `_append_skeleton_config_block`가 최소 골격(label + NET=仕切 자리표시)을 직접 쓴다. `form_template.md`에도 `[config]` 골격 포함. 실제 NET·교차검증 규칙은 채팅 **"규칙 반영"**(`apply_block_update`)으로 채운다.
- **form_03 정본 승격** — auto 마커 제거, `(정본 · build_form_types.py가 읽음)` 헤더로. JSON 불변(무손실).
- 잔여: **form_02**(미등록 draft, 블록 없음) — 업무규칙 확정 후 cold-start/규칙 반영으로 정본 블록 작성하면 마이그레이션 완전 종료.

### 선택지 (P2 이후, 별도 결정)

`config/form_types.json`을 끝내 **삭제**하고 phase4·테스트가 블록에서 직접 로드하게 할지 — 더 순수한 단일 소스지만 모든 reader를 건드림. P1/P2의 "생성물 유지" 방식이 그 전제(블록=정본)를 이미 달성하므로 삭제는 *순수성 vs blast radius* 판단으로 미룬다.

---

## 6. 가드레일 (비협상)

- 회계 숫자는 영원히 결정적 코드. 이 마이그레이션은 *룰이 어디 사는가*만 바꾼다 — 계산 로직·값 불변.
- 모든 전환은 골든/회귀 **무변동 증명** 후 진행. 정본 이동은 리팩터링이다.
- `build_form_types.py`는 임의 코드 실행 없음 — JSON 파싱 + 스키마 검증뿐.
- `if form_id == ...` 분기 영구 금지 원칙([phase4_calc.py:13](../scripts/phase4_calc.py#L13)) 유지.
