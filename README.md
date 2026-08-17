# tax-checker 배포 패키지

세금·건강보험료 판정 확인서 웹앱. Render(추천) 또는 다른 파이썬 호스팅에 그대로 올리면 됩니다.

## 폴더 구성
```
tax-checker/
├── app.py              ← Flask 진입점
├── etf_classifier.py   ← 네이버 시세/분류 API 연동
├── requirements.txt    ← 필요한 패키지 목록
└── static/
    └── tax-checker.html ← 실제 화면
```

## Render에 배포하기 (무료)

1. https://render.com 가입 (GitHub 계정으로 가입하면 편함)
2. 이 폴더를 GitHub 저장소로 올리기 (새 repo 만들고 이 안의 파일들 전부 push)
3. Render 대시보드 → "New +" → "Web Service" → 방금 만든 저장소 선택
4. 설정값:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app`
   - **Instance Type**: Free
5. Deploy 누르면 몇 분 뒤 `https://프로젝트이름.onrender.com` 같은 주소가 생김
6. 브라우저에서 `https://프로젝트이름.onrender.com/static/tax-checker.html` 열어서 확인

## 워드프레스에 끼워넣기

워드프레스 페이지에 "Custom HTML" 블록 추가 후:

```html
<iframe src="https://프로젝트이름.onrender.com/static/tax-checker.html"
        style="width:100%;height:900px;border:none;"></iframe>
```

## 배당수익률 수동 입력

`etf_classifier.py` 안의 `MANUAL_DIVIDEND_YIELD` 딕셔너리에 자주 보는 ETF 종목코드와
배당수익률(%)을 적어두면 화면에 자동으로 뜹니다. 값은 운용사 상품페이지에서 확인.

```python
MANUAL_DIVIDEND_YIELD = {
    "458730": 3.8,  # TIGER 미국배당다우존스
}
```

## 로컬에서 먼저 테스트하고 싶으면

```bash
pip install -r requirements.txt
python app.py
```
→ `http://127.0.0.1:5000/static/tax-checker.html` 접속

## 주의사항

- 이 앱은 네이버 금융 비공식 API를 씁니다. 페이지 구조가 바뀌면 시세/분류 조회가
  깨질 수 있습니다 (`/api/classify-ticker/_debug`로 가끔 점검 권장).
- 이 확인서는 참고용 계산기이며 국세청·건강보험공단의 공식 문서가 아닙니다.
