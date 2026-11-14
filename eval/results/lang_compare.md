# 신고 결과 번역 — 수정 전(before) / 후(after)

같은 분실 신고(검정 가죽 지갑, 지하철 2호선, 어제)를 6개 언어로 넣고 운영 실제 습득물 12건을 판정. 언어당 2회.
검사 = `apps/api/langcheck.lang_ok`(한글 잔존·목표 문자 체계·영어 문장 여부). 칸마다 '정상 / 다른 언어 섞임 / 빈칸(원문 표시)'.

| 언어 | 칸 | 수정 전 정상 | 수정 전 섞임 | 수정 후 정상 | 수정 후 섞임 | 수정 후 빈칸(원문) |
|---|---|---|---|---|---|---|
| en | 물품명 | 24/24 | 0 | 24/24 | 0 | 0 |
| en | 설명 | 24/24 | 0 | 14/24 | 0 | 10 |
| en | 판정 이유 | 24/24 | 0 | 24/24 | 0 | 0 |
| ja | 물품명 | 0/24 | 24 | 24/24 | 0 | 0 |
| ja | 설명 | 0/24 | 24 | 14/24 | 0 | 10 |
| ja | 판정 이유 | 12/24 | 12 | 23/24 | 0 | 1 |
| zh-CN | 물품명 | 24/24 | 0 | 24/24 | 0 | 0 |
| zh-CN | 설명 | 24/24 | 0 | 14/24 | 0 | 10 |
| zh-CN | 판정 이유 | 24/24 | 0 | 24/24 | 0 | 0 |
| zh-TW | 물품명 | 24/24 | 0 | 24/24 | 0 | 0 |
| zh-TW | 설명 | 23/24 | 1 | 14/24 | 0 | 10 |
| zh-TW | 판정 이유 | 22/24 | 2 | 24/24 | 0 | 0 |
| vi | 물품명 | 24/24 | 0 | 24/24 | 0 | 0 |
| vi | 설명 | 22/24 | 2 | 14/24 | 0 | 10 |
| vi | 판정 이유 | 12/24 | 12 | 24/24 | 0 | 0 |
| th | 물품명 | 24/24 | 0 | 24/24 | 0 | 0 |
| th | 설명 | 24/24 | 0 | 14/24 | 0 | 10 |
| th | 판정 이유 | 24/24 | 0 | 24/24 | 0 | 0 |

- before: AI 호출 12회 · 입력 15,542 · 출력 12,321 토큰, 판정 1회 평균 5.7초

- after: AI 호출 26회 · 입력 29,366 · 출력 12,797 토큰, 판정 1회 평균 3.8초

## 결과 언어 검사에 걸려 다시 쓴 값(after, 14칸)

| 언어 | 칸 | 처음 나온 값 | 다시 쓴 뒤(빈칸 = 원문 표시·근거 생략) |
|---|---|---|---|
| ja | reason | 색상이 브라운으로 불일치하며, 습득일과 지하철 장소만 일치함. | 色がブラウンで一致せず、拾得日と地下鉄の場所のみ一致しています。 |
| ja | reason | 검은색 지갑, 지하철(2호선 Sillim Station 부근) 습득으로 조건이 잘 부합함. | 黒の財布、地下鉄（2号線 Sillim Station 付近）での拾得で条件によく合致しています。 |
| ja | reason | 검은색이지만 지갑이 아닌 파우치이며 무선이어폰과 화장품이 들어있음. | 黒色ですが財布ではなくポーチであり、ワイヤレスイヤホンと化粧品が入っています。 |
| ja | reason | 피치오렌지색 여성지갑으로 색상이 불일치함. | ピーチオレンジ色の女性用財布で色が一致しません。 |
| ja | reason | 검은색 루이까또즈 카드지갑으로 지하철에서 습득되었고 카드 등이 들어있음. | 黒の Louis Quatorze のカードケースで、地下鉄で拾得されカードなどが入っています。 |
| ja | reason | 회색 카드지갑으로 색상이 불일치함. | グレーのカードケースで色が一致しません。 |
| ja | reason | 로열블루색 카드지갑으로 색상이 불일치함. | ロイヤルブルーのカードケースで色が一致しません。 |
| ja | reason | 검은색 카드지갑이나 습득 장소가 지하철이 아닌 기차임. | 黒のカードケースですが、拾得場所が地下鉄ではなく列車です。 |
| ja | reason | 검은색 지갑이지만 습득 장소가 지하철이 아닌 기차임. |  |
| ja | reason | 검은색 반지갑이지만 습득 장소가 지하철이 아닌 기차임. | 黒の二つ折り財布ですが、拾得場所が地下鉄ではなく列車です。 |
| ja | name_en | カードケース（身分증、カード） | カードケース（身分証明書、カード） |
| ja | reason | 검은색 카드지갑(신분증, 카드 포함)이나 습득 장소가 기차임. | 黒のカードケース（身分証明書、カード含む）ですが、拾得場所が列車です。 |
| ja | reason | 검은색 맥세이프 카드지갑이나 습득 장소가 기차임. | 黒の MagSafe カードケースですが、拾得場所が列車です。 |
| zh-TW | reason | 顏色為棕색與遺失的黑色不符，且內含身分證件與 Kakao Bank 卡。 | 顏色為棕色與遺失的黑色不符，且內含身份證件與 Kakao Bank 卡。 |

## 예시 (각 언어 첫 회, 상위 3건)

**en · before**
- 카드지갑(신분증,카드) → Black card wallet with cards / Black card wallet containing cards and ID found in Mokpo. / 85점 — High match for black card wallet containing credit cards and ID.
- 검정지갑 → Black wallet / Black wallet found in Busan. / 80점 — Strong match for black wallet, though location is Busan.
- 디올 지갑 → Dior black wallet / Black Dior wallet found in an unknown location. / 75점 — Matches item type (wallet) and color (black); specific brand (Dior) not mentioned by user.

**en · after**
- 디올 지갑 → Dior wallet / Found outside Exit 5 of Sillim Station / 80점 — Matches item type (wallet), color (black), place (subway, Sillim Station on Line 2), and date.
- 루이까또즈 카드지갑 → Louis Quatorze card wallet / Contains ID, cards, etc. / 75점 — Matches color (black), type (card wallet), found on subway, and contains cards.
- 카드지갑(신분증,카드) → Card wallet (ID, card) / - / 60점 — Matches color (black), wallet type, and contains cards/ID, but found on a train rather than subway.

**ja · before**
- 검정지갑 → Black Wallet / A general black wallet closely matching the lost item description. / 80점 — 紛失物である「黒い財布」に最もシンプルに一致するカテゴリと色です。
- 디올 지갑 → Dior Black Wallet / A black Dior wallet, matching the color and item type. / 75점 — 黒色の財布という点が一致しますが、ブランド（Dior）の特定情報が不足しています。
- 검정 반지갑 → Black Folding Wallet / A black bi-fold wallet that matches the specified color and item type. / 75점 — 黒色の二つ折り財布で、一般的な革財布の形状と一致する可能性が高いです。

**ja · after**
- 디올 지갑 → Dior 財布 / 新林駅5番出口の外で拾得 / 75점 — 黒の財布、地下鉄（2号線 Sillim Station 付近）での拾得で条件によく合致しています。
- 루이까또즈 카드지갑 → Louis Quatorze カードケース / 身分証、カードなど中身あり / 70점 — 黒の Louis Quatorze のカードケースで、地下鉄で拾得されカードなどが入っています。
- 카드지갑(신분증,카드) → カードケース（身分証明書、カード） / - / 55점 — 黒のカードケース（身分証明書、カード含む）ですが、拾得場所が列車です。

**zh-CN · before**
- 카드지갑(신분증,카드) → 黑色卡包（含证件卡片） / 木浦拾获的黑色卡包，内部装有卡片和证件，与失主内有信用卡的描述高度一致。 / 85점 — 颜色（黑色）一致，且明确备注含有卡片，与失主描述极为吻合。
- 검정지갑 → 黑色钱包 / 釜山拾获的黑色钱包，与失主描述的黑色钱包基本一致。 / 80점 — 颜色（黑色）与品类（钱包）高度吻合。
- 검정 반지갑 → 黑色短款钱包 / 蔚山拾获的黑色短款/折叠钱包，颜色和种类与失主描述非常契合。 / 80점 — 黑色皮质折叠钱包的可能性高，颜色与品类吻合。

**zh-CN · after**
- 디올 지갑 → Dior 钱包 / 在新林站 5 号出口室外拾获 / 80점 — 黑色钱包，拾得地点为地铁，与遗失物品特征一致；未注明具体内容物。
- 루이까또즈 카드지갑 → Louis Quatorze 卡包 / 内有身份证、卡等 / 75점 — 黑色卡包，拾得地点为地铁，内含身份证件及卡类，特征高度相似。
- 검정지갑 → 黑色钱包 / - / 65점 — 黑色钱包，颜色及种类一致，但拾得地点为火车。

**zh-TW · before**
- 카드지갑(신분증,카드) → 黑色卡包（含卡片證件） / 黑色卡包，標註含有證件及卡片，與失主描述的內部物品高度符合。 / 85점 — 黑色卡包且內含證件與信用卡，與失主提到的「裡面有信用卡」高度吻合。
- 검정지갑 → 黑色錢包 / 黑色通用錢包，與遺失的黑色皮夾特徵一致。 / 80점 — 黑色錢包，顏色與品項均高度吻合失主描述。
- 디올 지갑 → 迪奧黑色皮夾 / 黑色Dior錢包，地點不詳，顏色與錢包種類 matches 遺失物。 / 75점 — 黑色迪奧錢包，顏色與種類一致，但缺乏具體材質資訊。

**zh-TW · after**
- 루이까또즈 카드지갑 → Louis Quatorze 卡片夾 / 內含身分證、卡片等 / 80점 — Louis Quatorze黑色卡包，於地鐵拾得，內含身分證與卡片，與遺失特徵高度相符。
- 디올 지갑 → Dior 錢包 / 於新林站 5 號出口外部拾獲 / 75점 — 黑色皮夾且拾得地點為地鐵，顏色與種類相符；但未註明內部內容物。
- 카드지갑(신분증,카드) → 卡片夾（身分證、卡片） / - / 50점 — 黑色卡包且內含卡片與身分證，但拾得地點為火車而非地鐵。

**vi · before**
- 카드지갑(신분증,카드) → Ví đựng thẻ màu đen (có thẻ/ID) / Ví đựng thẻ màu đen bên trong có chứa thẻ tín dụng và giấy tờ tùy thân. / 70점 — Ví đựng thẻ màu đen có chứa thẻ và giấy tờ bên trong, rất giống mô tả.
- 검정지갑 → Ví màu đen / Ví màu đen được tìm thấy tại Busan. / 65점 — Ví màu đen, trùng khớp hoàn toàn về loại vật phẩm và màu sắc cơ bản.
- 검정 반지갑 → Ví gập màu đen / Ví gập (반지갑) màu đen được tìm thấy tại Ulsan. / 65점 — Ví gập màu đen, khả năng cao trùng khớp với ví da màu đen bị mất.

**vi · after**
- 디올 지갑 → Ví Dior / Nhặt được ở bên ngoài Cổng 5 ga Sillim / 70점 — Ví Dior màu đen được tìm thấy ở tàu điện ngầm, khớp về màu sắc và nơi nhặt được.
- 루이까또즈 카드지갑 → Ví đựng thẻ Louis Quatorze / Bên trong có giấy tờ tùy thân, thẻ, v.v. / 65점 — Ví đựng thẻ Louis Quatorze màu đen nhặt ở tàu điện ngầm, bên trong có chứa thẻ.
- 카드지갑(신분증,카드) → Ví đựng thẻ (giấy tờ tùy thân, thẻ) / - / 55점 — Ví đựng thẻ màu đen chứa thẻ và giấy tờ, nhưng nhặt được trên tàu hỏa.

**th · before**
- 카드지갑(신분증,카드) → กระเป๋าใส่บัตรสีดำพร้อมบัตร / กระเป๋าใส่บัตรสีดำที่มีบัตรประชาชนและบัตรเครดิตข้างใน / 80점 — กระเป๋าใส่บัตรสีดำที่มีบัตรเครดิตและบัตรประชาชนอยู่ข้างใน ตรงตามรายละเอียด
- 디올 지갑 → กระเป๋าสตางค์ Dior สีดำ / กระเป๋าสตางค์สีดำแบรนด์ Dior ไม่ระบุสถานที่พบ / 75점 — เป็นกระเป๋าสตางค์สีดำตรงตามที่ระบุ แต่ไม่ระบุแบรนด์ในคำขอ
- 검정지갑 → กระเป๋าสตางค์สีดำ / กระเป๋าสตางค์สีดำทั่วไปที่เก็บได้ในปูซาน / 70점 — ตรงว่าเป็นกระเป๋าสตางค์สีดำ แต่สถานที่เก็บได้คือปูซาน

**th · after**
- 디올 지갑 → กระเป๋าสตางค์ Dior / เก็บได้ที่ด้านนอกทางออก 5 สถานี Sillim / 75점 — เป็นกระเป๋าสตางค์สีดำ เก็บได้ในรถใต้ดินตรงตามที่ระบุ แต่มีการระบุแบรนด์ Dior
- 루이까또즈 카드지갑 → กระเป๋าใส่บัตร Louis Quatorze / มีบัตรประจำตัวประชาชน, บัตร ฯลฯ อยู่ข้างใน / 70점 — กระเป๋าใส่บัตรสีดำ เก็บได้ในรถใต้ดิน มีบัตรอยู่ข้างในตรงตามที่ระบุ
- 검정지갑 → กระเป๋าสตางค์สีดำ / - / 50점 — เป็นกระเป๋าสตางค์สีดำ แต่สถานที่เก็บได้คือบนรถไฟ ไม่ใช่รถใต้ดิน
