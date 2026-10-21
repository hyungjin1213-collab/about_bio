-- K-Bio Community: 학생 이메일 인증 (Supabase SQL Editor에서 한 번 실행)
-- 허용 조건: 메일 주소의 @ 뒤 도메인이 정확히 ".ac.kr"로 끝나는 경우만 가입 가능.

-- 1) 가입 전 검사: 가입 시도 자체를 데이터베이스에서 막습니다 (화면 검사만으로는 부족).
create or replace function public.require_ac_kr_email()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if new.email is null
     or lower(new.email) !~ '@[a-z0-9-]+(\.[a-z0-9-]+)*\.ac\.kr$' then
    raise exception 'ac.kr 학교 메일로만 가입할 수 있습니다.';
  end if;
  return new;
end;
$$;

drop trigger if exists require_ac_kr_email on auth.users;
create trigger require_ac_kr_email
  before insert on auth.users
  for each row execute function public.require_ac_kr_email();

-- 2) 가입자 정보: 메일 주소는 저장하지 않고, 학교 도메인만 기록합니다.
create table if not exists public.profiles (
  user_id    uuid primary key references auth.users (id) on delete cascade,
  school     text not null,          -- 메일 도메인 (예: snu.ac.kr)
  role       text not null default 'student' check (role in ('student', 'professor')),
  created_at timestamptz not null default now()
);

create or replace function public.create_profile()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.profiles (user_id, school)
  values (new.id, split_part(lower(new.email), '@', 2));
  return new;
end;
$$;

drop trigger if exists create_profile on auth.users;
create trigger create_profile
  after insert on auth.users
  for each row execute function public.create_profile();

-- 3) 보안: 누구나 자기 프로필만 읽을 수 있고, 직접 쓰기는 막습니다.
alter table public.profiles enable row level security;

drop policy if exists "read own profile" on public.profiles;
create policy "read own profile" on public.profiles
  for select using (auth.uid() = user_id);
