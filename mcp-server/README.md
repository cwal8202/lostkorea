# LostKorea MCP

> **보통은 이 프로그램이 필요 없어요.** AI 앱에 원격 주소 **`https://lostkorea.com/mcp`**를 붙여 넣으면 돼요
> (연결 방법: https://lostkorea.com/ai-tools). 이 폴더는 내 PC에서 직접 돌리거나 시험할 때 쓰는 실행판이에요.

AI(Claude 등 MCP를 쓰는 AI)가 LostKorea의 습득물 AI 매칭을 **도구로** 쓰게 하는 MCP 서버예요.
경찰청 공개 습득물(최근 약 6개월, 경찰서·유실물센터)과 분실 신고를 이어 줍니다.
기관·연구용으로 분실 신고 묶음을 한꺼번에 돌려 보거나, 실제 분실 사례를 모아 매칭해 볼 때 씁니다.

## 도구

| 도구 | 하는 일 | 비용 |
|---|---|---|
| `find_matches` | 신고 1~20건(어느 언어든) → 웹과 같은 AI 매칭(추출 → 이동 경로 지역 → 검색 → 판정). 신고마다 요약(strong 80점↑ / weak 50~79 / none), AI가 뽑은 항목, 검색한 지역, 후보(점수·근거·장소/날짜 이어짐·보관 기관·전화·사진·경찰청 원본·지도, 한국어+영어). **저장·알림 없음** | 신고 1건 ≈ $0.004 |
| `search_found_items` | 키워드·지역·날짜·사진 있음으로 찾기(전체 습득물 화면과 같음). 표기만 다른 말은 같이, 브랜드 → 일반 이름은 `include_similar_names` | 없음 |
| `get_found_item` | 습득물 한 건 + 경찰 쪽 **지금 상태**(보관중·종결) | 없음 |

결과는 **후보**예요. 주인은 보관 기관에 전화·방문해 직접 확인해야 해요.

## 붙이기

필요한 것: [uv](https://docs.astral.sh/uv/), 운영자가 운영 화면에서 발급한 **기관 키**(`find_matches`·`get_found_item`에 필요).
원격 주소에서도 같은 키를 머리글 `X-MCP-Key`로 넣으면 키 한도로 쓸 수 있어요. Claude 앱(웹·데스크톱) 커넥터처럼 머리글을 못 넣는
곳은 주소를 `https://lostkorea.com/mcp?key=키`로 — 이 주소는 키와 같으니 남에게 보이지 않게.

**Claude Code**
```bash
claude mcp add lostkorea -e LOSTKOREA_MCP_KEY=<키> -- uv run --script <이 폴더>/lostkorea_mcp.py
```

**Claude Desktop** (`claude_desktop_config.json`)
```json
{
  "mcpServers": {
    "lostkorea": {
      "command": "uv",
      "args": ["run", "--script", "<이 폴더>/lostkorea_mcp.py"],
      "env": { "LOSTKOREA_MCP_KEY": "<키>" }
    }
  }
}
```

환경변수: `LOSTKOREA_MCP_KEY`(필수), `LOSTKOREA_API_URL`(기본 `https://lostkorea.com`).
의존성은 파일 머리의 PEP 723 블록(`mcp==2.3.0`, `httpx`) — `uv run --script`가 알아서 설치해요.

## 한도·개인정보

- 키 한도는 운영자가 키마다 정해요(보통 하루 신고 300건). 시간당 습득물 상태 확인 600건. 신고 1건은 서비스 하루 AI 상한에서도 1단위.
- 서버가 신고 문장의 전화번호·카드번호·주민번호·여권번호·이메일을 AI로 보내기 전에 가려요. 신고는 저장하지 않아요.
- 한 번에 많이 보내면 MCP가 5건씩 나눠 보내요(신고 1건 약 9초, 5건 동시 → 요청 하나 약 15초).
