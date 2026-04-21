'use client'

import { useState } from 'react'
import Image from 'next/image'
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { createClient } from '@/lib/supabase/client'

const inputCls =
  'w-full rounded-lg border border-[#E8E8E8] bg-[#F5F5F5] px-3 text-sm text-gray-800 ' +
  'placeholder:text-gray-400 transition-colors ' +
  'focus:border-[#1B3A6B] focus:bg-white focus:outline-none focus:ring-2 focus:ring-[#1B3A6B]/10 ' +
  'disabled:cursor-not-allowed disabled:opacity-50'

export default function LoginForm({ resetSuccess }: { resetSuccess?: boolean }) {
  const [email, setEmail]         = useState('')
  const [password, setPassword]   = useState('')
  const [error, setError]         = useState<string | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const router = useRouter()

  const handleSubmit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    setError(null)
    setIsLoading(true)

    const supabase = createClient()
    const { error: authError } = await supabase.auth.signInWithPassword({ email, password })

    if (authError) {
      setError(authError.message)
      setIsLoading(false)
      return
    }

    router.push('/chat')
    router.refresh()
  }

  return (
    <div className="w-full sm:max-w-[480px]">
      {/* Card */}
      <div className="rounded-2xl bg-white shadow-xl ring-1 ring-black/5 px-5 py-8 sm:p-[44px]">

        {/* Back to home — inside card, top */}
        <Link href="/" className="mb-5 block text-[13px] text-gray-400 hover:text-gray-600">
          ← Back to home
        </Link>

        {/* Password-reset success banner */}
        {resetSuccess && (
          <div className="mb-5 rounded-lg bg-green-50 px-3 py-2.5 text-xs text-green-700 ring-1 ring-green-200 flex items-center gap-2">
            <svg className="h-4 w-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
            </svg>
            Password updated successfully. Sign in with your new password.
          </div>
        )}

        {/* Logo */}
        <div className="mb-5 flex justify-center">
          <Image src="/njdot_logo.png" alt="NJDOT" width={64} height={64} priority />
        </div>

        <h1 className="mb-1 text-center text-xl font-bold text-[#1B3A6B]">Smart Assistant</h1>
        <p className="mb-6 text-center text-sm text-gray-400">Sign in to your account</p>

        <form onSubmit={handleSubmit} className="space-y-4">
          {/* Email */}
          <div>
            <label htmlFor="email" className="mb-1.5 block text-xs font-medium text-gray-700">
              Email address
            </label>
            <input
              id="email"
              type="email"
              autoComplete="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              disabled={isLoading}
              placeholder="you@example.com"
              style={{ height: '44px' }}
              className={inputCls}
            />
          </div>

          {/* Password */}
          <div>
            <div className="mb-1.5 flex items-center justify-between">
              <label htmlFor="password" className="text-xs font-medium text-gray-700">
                Password
              </label>
              <Link href="/forgot-password" className="text-xs text-[#CC2529] hover:underline">
                Forgot password?
              </Link>
            </div>
            <input
              id="password"
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              disabled={isLoading}
              placeholder="••••••••"
              style={{ height: '44px' }}
              className={inputCls}
            />
          </div>

          {/* Error */}
          {error && (
            <div className="rounded-lg bg-red-50 px-3 py-2.5 text-xs text-red-700 ring-1 ring-red-200">
              {error}
            </div>
          )}

          {/* Submit */}
          <button
            type="submit"
            disabled={isLoading}
            style={{ height: '44px' }}
            className="w-full rounded-lg bg-[#CC2529] text-sm font-semibold text-white transition-colors hover:bg-[#a81e21] focus:outline-none focus:ring-2 focus:ring-[#CC2529]/30 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {isLoading ? 'Signing in…' : 'Sign In'}
          </button>
        </form>

        <p className="mt-5 text-center text-xs text-gray-500">
          Don&apos;t have an account?{' '}
          <Link href="/signup" className="font-semibold text-[#1B3A6B] hover:underline">
            Sign up
          </Link>
        </p>
      </div>
    </div>
  )
}
