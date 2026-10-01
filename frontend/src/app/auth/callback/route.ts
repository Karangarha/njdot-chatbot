// Email-confirmation landing: swaps the PKCE ?code from the Supabase link for a session cookie.
import { NextResponse, type NextRequest } from 'next/server'
import { createClient } from '@/lib/supabase/server'

export async function GET(request: NextRequest) {
  const { origin, searchParams } = request.nextUrl
  const code = searchParams.get('code')

  if (code) {
    const supabase = await createClient()
    const { error } = await supabase.auth.exchangeCodeForSession(code)
    if (!error) return NextResponse.redirect(`${origin}/chat`)
  }

  // Expired/used link, or opened in a different browser than the one that signed up.
  return NextResponse.redirect(`${origin}/login`)
}
