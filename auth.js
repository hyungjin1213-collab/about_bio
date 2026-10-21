// 학생 이메일 인증 로그인 (Supabase). 설정값은 supabase-config.js에서 가져옵니다.
// 가입과 로그인이 같은 흐름입니다: 학교 메일로 인증 링크를 받아 누르면 끝납니다.
(function () {
  const cfg = window.SUPABASE_CONFIG || {};
  const ready = !!(cfg.url && cfg.publishableKey && window.supabase);
  const client = ready ? window.supabase.createClient(cfg.url, cfg.publishableKey) : null;

  const btn = document.getElementById("login-btn");
  const modal = document.getElementById("login-modal");
  const form = document.getElementById("login-form");
  const email = document.getElementById("login-email");
  const msg = document.getElementById("login-msg");
  const close = document.getElementById("login-close");

  // 화면 검사는 편의용입니다. 실제 검사는 데이터베이스(schema.sql)에서 합니다.
  const isSchoolEmail = (v) => /@[a-z0-9-]+(\.[a-z0-9-]+)*\.ac\.kr$/i.test(v.trim());

  function show(text, ok) {
    msg.textContent = text;
    msg.style.color = ok ? "#86efac" : "#fca5a5";
  }

  function render(user) {
    btn.textContent = user ? "로그아웃" : "로그인";
    btn.dataset.mode = user ? "logout" : "login";
    btn.title = user ? (user.email || "") : "학교 메일로 로그인";
  }

  if (!client) {
    btn.textContent = "로그인 준비중";
    btn.disabled = true;
    return;
  }

  function refresh() {
    client.auth.getSession().then(({ data }) => {
      const user = data.session && data.session.user;
      render(user);
      if (user) {
        // 메일 링크를 다른 탭에서 눌러 로그인했을 때 이 창도 바로 바뀌게 합니다.
        modal.hidden = true;
        show("", true);
      }
    });
  }

  refresh();
  client.auth.onAuthStateChange((_event, session) => render(session && session.user));
  // 인증 링크는 보통 새 탭에서 열리므로, 이 창으로 돌아오거나 다른 탭에서 로그인이 바뀌면 다시 확인합니다.
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  window.addEventListener("focus", refresh);
  window.addEventListener("storage", refresh);

  btn.addEventListener("click", async () => {
    if (btn.dataset.mode === "logout") {
      await client.auth.signOut();
      return;
    }
    modal.hidden = false;
    email.focus();
  });

  close.addEventListener("click", () => { modal.hidden = true; });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const value = email.value.trim();
    if (!isSchoolEmail(value)) {
      show("ac.kr로 끝나는 학교 메일을 입력해 주세요.", false);
      return;
    }
    show("인증 메일을 보내는 중…", true);
    const { error } = await client.auth.signInWithOtp({
      email: value,
      options: { shouldCreateUser: true, emailRedirectTo: location.origin + location.pathname },
    });
    if (error) {
      show("메일을 보내지 못했습니다: " + error.message, false);
      return;
    }
    show("학교 메일로 인증 링크를 보냈습니다. 메일의 링크를 누른 뒤 이 창으로 돌아오면 자동으로 로그인됩니다.", true);
  });
})();
