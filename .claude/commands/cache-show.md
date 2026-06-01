# 캐시 현황 표시

`$ARGUMENTS`에 선택적으로 필터를 전달할 수 있다. (예: `/cache-show`, `/cache-show retailer`, `/cache-show product`)

`mappings/ocr_retailer.csv`와 `mappings/ocr_product.csv`를 읽어 현황을 표시한다.

## 표시 형식

```
■ 소매처 캐시 (ocr_retailer.csv) — N건
  OCR 得意先名                    → 소매처코드
  ローソントウカイ (1991474)      → 1991474
  ...

■ 제품 캐시 (ocr_product.csv) — N건
  OCR 商品名                      → 제품코드  | 마스터명
  チャパゲティ 140g               → 101000551 | チャパゲティー1P
  ...
```

`$ARGUMENTS`가 `retailer`이면 소매처 캐시만, `product`이면 제품 캐시만 표시한다.
파일이 비어있으면 "캐시 없음" 표시.
