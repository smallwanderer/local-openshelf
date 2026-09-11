# Dotori Workspace SPA

Django API에 단계적으로 연결하는 React·TypeScript·Vite SPA입니다. 문서 목록·상세·파싱 본문·AI 준비 상태·다운로드뿐 아니라 업로드·폴더·이동·즐겨찾기·휴지통·복구·AI 재처리도 실제 Django API를 기본으로 사용합니다. 검색 결과와 RAG 답변은 후속 단계까지 `src/api/workspace.ts`의 adapter가 제공하는 목업을 사용합니다.

백엔드 없이 문서 화면만 확인하려면 `VITE_USE_MOCK_API=true`로 실행할 수 있습니다. 아직 API가 없는 워크스페이스와 공유 UI는 향후 확장 지점으로 유지하고 화면에서 `예정`으로 표시합니다.

```bash
cd web
npm ci
npm run dev
```

브라우저에서 `http://127.0.0.1:4173`을 엽니다. 프로덕션 정적 결과물 확인은 `npm run build`로 수행합니다.

- `src/api/`: HTTP, 공통 모델, workspace adapter
- `src/components/`: 공통 상태와 표시 컴포넌트
- `src/features/`: 문서, 검색, 채팅, 설정, 홈 기능
- `src/i18n/`: 한국어·영어 화면 문구
