/**
 * Supabase auth middleware for Next.js App Router.
 *
 * Responsibilities:
 *  1. Refresh the Supabase session on every request so cookies stay valid.
 *  2. Protect /chat — redirect unauthenticated visitors to /login.
 *  3. Redirect authenticated visitors away from auth pages to /chat.
 */
import { createServerClient } from '@supabase/ssr'
import { NextResponse, type NextRequest } from 'next/server'

// Public auth-related paths (accessible without login, but redirected away from if logged in)
const AUTH_PATHS = ['/login', '/signup', '/forgot-password']

// Paths that should always be accessible regardless of auth state
const ALWAYS_PUBLIC = ['/update-password']

export async function middleware(request: NextRequest) {
  let supabaseResponse = NextResponse.next({ request })

  const supabase = createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY!,
    {
      cookies: {
        getAll() {
          return request.cookies.getAll()
        },
        setAll(cookiesToSet) {
          cookiesToSet.forEach(({ name, value }) =>
            request.cookies.set(name, value),
          )
          supabaseResponse = NextResponse.next({ request })
          cookiesToSet.forEach(({ name, value, options }) =>
            supabaseResponse.cookies.set(name, value, options),
          )
        },
      },
    },
  )

  // IMPORTANT: always use getUser() — getSession() is not safe in middleware.
  const {
    data: { user },
  } = await supabase.auth.getUser()

  const { pathname } = request.nextUrl

  // ── Always-public paths (e.g. /reset-password needs the token in the URL) ──
  if (ALWAYS_PUBLIC.some((p) => pathname.startsWith(p))) {
    return supabaseResponse
  }

  // ── Protect /chat ─────────────────────────────────────────────────────────
  if (pathname.startsWith('/chat') && !user) {
    const loginUrl = request.nextUrl.clone()
    loginUrl.pathname = '/login'
    return NextResponse.redirect(loginUrl)
  }

  // ── Redirect authenticated users away from auth pages ────────────────────
  if (AUTH_PATHS.includes(pathname) && user) {
    const chatUrl = request.nextUrl.clone()
    chatUrl.pathname = '/chat'
    return NextResponse.redirect(chatUrl)
  }

  return supabaseResponse
}

export const config = {
  matcher: [
    '/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp)$).*)',
  ],
}
