const firebaseConfig = {
  apiKey: "AIzaSyCDRB1vQ6M2U7dAqThL7vmI5GEDYp-CxJc",
  authDomain: "math-app-dfe8d.firebaseapp.com",
  projectId: "math-app-dfe8d",
  storageBucket: "math-app-dfe8d.firebasestorage.app",
  messagingSenderId: "735740019043",
  appId: "1:735740019043:web:95d75289075db9a5fa5356",
  measurementId: "G-DHSHV4WDK1",
};

const sdk = {
  app: null,
  auth: null,
  firestore: null,
};

const syncState = {
  ready: false,
  loading: false,
  error: "",
  user: null,
  counts: {documents: 0, problems: 0, exams: 0},
  lastPushAt: localStorage.getItem("mathbank-firebase-last-push") || "",
  unsubscribers: [],
};

let sdkPromise = null;
let statusCallback = () => {};

function emitStatus() {
  statusCallback({...syncState, user: syncState.user ? {...syncState.user} : null});
  window.dispatchEvent(new CustomEvent("mathbank-firebase-status", {detail: syncState}));
}

async function loadSdk() {
  if (sdkPromise) return sdkPromise;
  syncState.loading = true;
  emitStatus();
  sdkPromise = Promise.all([
    import("https://www.gstatic.com/firebasejs/10.12.5/firebase-app.js"),
    import("https://www.gstatic.com/firebasejs/10.12.5/firebase-auth.js"),
    import("https://www.gstatic.com/firebasejs/10.12.5/firebase-firestore.js"),
  ]).then(([appMod, authMod, firestoreMod]) => {
    sdk.app = appMod.getApps().length ? appMod.getApp() : appMod.initializeApp(firebaseConfig);
    sdk.auth = authMod;
    sdk.firestore = firestoreMod;
    syncState.ready = true;
    syncState.loading = false;
    syncState.error = "";
    return {appMod, authMod, firestoreMod};
  }).catch(error => {
    syncState.ready = false;
    syncState.loading = false;
    syncState.error = `Firebase SDK를 불러오지 못했습니다: ${error.message}`;
    emitStatus();
    throw error;
  });
  return sdkPromise;
}

function appAuth() {
  return sdk.auth.getAuth(sdk.app);
}

function appDb() {
  return sdk.firestore.getFirestore(sdk.app);
}

function userRoot() {
  if (!syncState.user) throw new Error("Firebase에 먼저 로그인해 주세요.");
  return ["users", syncState.user.uid, "mathbank"];
}

function collectionPath(name) {
  return [...userRoot(), name];
}

function sanitizeForFirestore(value) {
  if (value === undefined) return null;
  if (value === null || typeof value !== "object") return value;
  if (Array.isArray(value)) return value.map(sanitizeForFirestore);
  return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, sanitizeForFirestore(item)]));
}

async function writeInChunks(items, writer, chunkSize = 450) {
  for (let index = 0; index < items.length; index += chunkSize) {
    await Promise.all(items.slice(index, index + chunkSize).map(writer));
  }
}

function stopLiveListeners() {
  for (const unsubscribe of syncState.unsubscribers) unsubscribe();
  syncState.unsubscribers = [];
}

function startLiveListeners() {
  stopLiveListeners();
  if (!syncState.user || !sdk.firestore) return;
  const db = appDb();
  const {collection, onSnapshot} = sdk.firestore;
  for (const name of ["documents", "problems", "exams"]) {
    const ref = collection(db, ...collectionPath(name));
    syncState.unsubscribers.push(onSnapshot(ref, snapshot => {
      syncState.counts[name] = snapshot.size;
      emitStatus();
    }, error => {
      syncState.error = `Firestore 실시간 연결 실패: ${error.message}`;
      emitStatus();
    }));
  }
}

export async function initFirebaseSync(options = {}) {
  statusCallback = options.onStatusChange || statusCallback;
  await loadSdk();
  const auth = appAuth();
  sdk.auth.onAuthStateChanged(auth, user => {
    syncState.user = user ? {
      uid: user.uid,
      email: user.email || "",
      name: user.displayName || user.email || "Firebase 사용자",
    } : null;
    if (user) startLiveListeners();
    else stopLiveListeners();
    emitStatus();
  });
  emitStatus();
  return syncState;
}

export async function signInFirebase() {
  await loadSdk();
  const provider = new sdk.auth.GoogleAuthProvider();
  provider.setCustomParameters({prompt: "select_account"});
  await sdk.auth.signInWithPopup(appAuth(), provider);
}

export async function signOutFirebase() {
  await loadSdk();
  await sdk.auth.signOut(appAuth());
}

export async function pushLocalSnapshot(api) {
  await loadSdk();
  if (!syncState.user) throw new Error("Firebase에 먼저 로그인해 주세요.");
  const snapshot = await api("/api/sync/export");
  const db = appDb();
  const {doc, setDoc, serverTimestamp} = sdk.firestore;
  const root = userRoot();
  const stamp = {synced_at: serverTimestamp(), owner_uid: syncState.user.uid};

  await writeInChunks(snapshot.documents || [], item => setDoc(
    doc(db, ...root, "documents", String(item.id)),
    sanitizeForFirestore({...item, ...stamp}),
    {merge: true},
  ));
  await writeInChunks(snapshot.problems || [], item => setDoc(
    doc(db, ...root, "problems", String(item.id)),
    sanitizeForFirestore({...item, ...stamp}),
    {merge: true},
  ));
  await writeInChunks(snapshot.exams || [], item => setDoc(
    doc(db, ...root, "exams", String(item.id)),
    sanitizeForFirestore({...item, ...stamp}),
    {merge: true},
  ));
  await setDoc(doc(db, ...root, "meta", "lastLocalSnapshot"), sanitizeForFirestore({
    project: "MathBank Studio",
    pushed_at: new Date().toISOString(),
    counts: {
      documents: (snapshot.documents || []).length,
      problems: (snapshot.problems || []).length,
      exams: (snapshot.exams || []).length,
    },
    owner_uid: syncState.user.uid,
    synced_at: serverTimestamp(),
  }), {merge: true});
  syncState.lastPushAt = new Date().toISOString();
  localStorage.setItem("mathbank-firebase-last-push", syncState.lastPushAt);
  emitStatus();
  return snapshot;
}

export async function syncProblem(problem) {
  await loadSdk();
  if (!syncState.user || !problem?.id) return;
  const db = appDb();
  const {doc, setDoc, serverTimestamp} = sdk.firestore;
  await setDoc(
    doc(db, ...collectionPath("problems"), String(problem.id)),
    sanitizeForFirestore({...problem, owner_uid: syncState.user.uid, synced_at: serverTimestamp()}),
    {merge: true},
  );
}

export async function deleteCloudProblem(id) {
  await loadSdk();
  if (!syncState.user || !id) return;
  const db = appDb();
  const {doc, deleteDoc} = sdk.firestore;
  await deleteDoc(doc(db, ...collectionPath("problems"), String(id)));
}

export async function deleteCloudProblems(ids) {
  await Promise.all((ids || []).map(id => deleteCloudProblem(id)));
}

export function firebaseSettingsPanel() {
  const userLine = syncState.user
    ? `<b>${syncState.user.name}</b><span>${syncState.user.email || "로그인됨"}</span>`
    : `<b>로그인이 필요합니다</b><span>Google 계정으로 로그인하면 Firestore 실시간 동기화가 시작됩니다.</span>`;
  const badge = syncState.user ? "connected" : syncState.ready ? "saved" : "";
  const badgeLabel = syncState.user ? "동기화 연결됨" : syncState.loading ? "연결 준비 중" : "로그인 필요";
  const countLine = syncState.user
    ? `클라우드에 문서 ${syncState.counts.documents}개 · 문제 ${syncState.counts.problems}개 · 시험지 ${syncState.counts.exams}개`
    : "로그인 후 이 컴퓨터의 문제 데이터를 Firebase로 올릴 수 있습니다.";
  const lastPush = syncState.lastPushAt
    ? new Intl.DateTimeFormat("ko-KR", {month:"short", day:"numeric", hour:"2-digit", minute:"2-digit"}).format(new Date(syncState.lastPushAt))
    : "아직 없음";
  return `<article class="panel settings-card firebase-card">
    <div class="settings-head">
      <div class="settings-logo firebase-logo">F</div>
      <div><span>FIREBASE SYNC</span><h2>실시간 동기화</h2><p>문제 데이터는 Firestore에, 이후 단계에서 원본 이미지와 도형은 Storage에 저장하도록 확장합니다.</p></div>
      <span class="connection-badge ${badge}">${badgeLabel}</span>
    </div>
    <div class="firebase-user-line">${userLine}</div>
    <div class="connection-detail ${syncState.error ? "error" : "success"}"><b>동기화 상태</b><span>${syncState.error || countLine}</span><span>마지막 올리기: ${lastPush}</span></div>
    <div class="settings-actions">
      ${syncState.user ? '<button type="button" class="ghost-btn" id="firebase-signout">로그아웃</button><button type="button" class="primary-btn" id="firebase-push-local">현재 데이터 Firebase에 올리기</button>' : '<button type="button" class="primary-btn" id="firebase-signin">Google로 로그인</button>'}
    </div>
  </article>`;
}
