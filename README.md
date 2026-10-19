# mail-strainer

Gmail 스팸 분류기. [gws](https://github.com/googleworkspace/cli)로 메일을 읽고, OpenRouter의 Jev(`typesafe/jev-router`)로 내 기준에 맞춰 스팸을 골라 SQLite에 저장한다. 로컬 웹 UI에서 결과를 보고, 정정하고, Gmail 스팸함으로 옮길 수 있다.

## 설치

```sh
brew install hoonkim/tap/mail-strainer
npm install -g @googleworkspace/cli   # gws
gws auth login                         # gmail.modify 권한 필요
mail-strainer setup                    # OpenRouter API 키 저장
```

## 사용법

```sh
mail-strainer serve --open                      # 웹 UI (http://127.0.0.1:8765)
mail-strainer fetch                             # 최근 3일 받은편지함 분류
mail-strainer fetch --move-spam 0.8             # 분류 후 확신도 80% 이상 스팸을 스팸함으로
mail-strainer move-spam --dry-run               # 옮길 대상 미리보기
mail-strainer doctor                            # gws 응답·키 진단
```

- 스팸 기준(내 프로젝트 설명 등)은 웹 UI의 "스팸 기준 프롬프트"에서 수정한다.
- UI에서 "스팸 아님/스팸으로"로 정정한 사례는 다음 분류 때 예시로 쓰인다.
- 스팸함으로 옮긴 메일을 "스팸 아님"으로 정정하면 받은편지함으로 돌아간다.

## 데이터

`~/.config/mail-strainer/`에 `.env`(API 키)와 `strainer.db`가 저장된다. `MAIL_STRAINER_HOME`으로 위치를 바꿀 수 있다.
