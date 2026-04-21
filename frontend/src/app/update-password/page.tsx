'use client'

import { useEffect, useState } from 'react'
import Image from 'next/image'
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { createClient } from '@/lib/supabase/client'
import { changePassword } from '@/lib/api'

type Stage = 'checking' | 'ready' | 'success'

const inputCls =
  'w-full rounded-lg border border-[#E8E8E8] bg-[#F5F5F5] px-3 text-sm text-gray-800 ' +
  'placeholder:text-gray-400 transition-colors ' +
  'focus:border-[#1B3A6B] focus:bg-white focus:outline-none focus:ring-2 focus:ring-[#1B3A6B]/10 ' +
  'disabled:cursor-not-allowed disabled:opacity-50'

export default function UpdatePasswordPage() {
  const [stage, setStage]         = useState<Stage>('checking')
  const [accessToken, setAccessToken] = useState('')
  const [password, setPassword]   = useState('')
  const [confirm, setConfirm]     = useState('')
  const [error, setError]         = useState<string | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const router = useRouter()

  useEffect(() => {
    const supabase = createClient()
    supabase.auth.getSession().then(({ data }) => {
      const token = data.session?.access_token
      if (token) {
        setAccessToken(token)
        setStage('ready')
      } else {
        // Not authenticated — send to forgot-password
        router.replace('/forgot-password')
      }
    })
  }, [router])

  const handleSubmit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    setError(null)

    if (password.length < 8) {
      setError('Password must be at least 8 characters.')
      return
    }
    if (password !== confirm) {
      setError('Passwords do not match.')
      return
    }

    setIsLoading(true)
    try {
      await changePassword(password, accessToken)
      setStage('success')
      setTimeout(() => router.push('/chat'), 2000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Password update failed. Please try again.')
    } finally {
      setIsLoading(false)
    }
  }

  const shell = (children: React.ReactNode) => (
    <main className="flex min-h-screen items-center justify-center bg-[#F5F5F5] px-4 py-12">
      <div className="w-full sm:max-w-[480px]">
        <div className="rounded-2xl bg-white shadow-xl ring-1 ring-black/5 px-5 py-8 sm:p-[44px]">
          {children}
        </div>
      </div>
    </main>
  )

  if (stage === 'checking') {
    return shell(
      <div className="flex flex-col items-center gap-4 py-4 text-center">
        <svg className="h-8 w-8 animate-spin text-[#1B3A6B]" fill="none" viewBox="0 0 24 24">
          <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
          <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
        </svg>
        <p className="text-sm text-gray-500">Checking your session…</p>
      </div>
    )
  }

  if (stage === 'success') {
    return shell(
      <div className="flex flex-col items-center text-center">
        <div className="mb-4 flex h-14 w-14 items-center justify-center rounded-full bg-green-100">
          <svg className="h-7 w-7 text-green-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
          </svg>
        </div>
        <h2 className="mb-2 text-lg font-bold text-[#1B3A6B]">Password updated!</h2>
        <p className="text-sm text-gray-500">Redirecting you back…</p>
      </div>
    )
  }

  return shell(
    <>
      <Link href="/chat" className="mb-5 block text-[13px] text-gray-400 hover:text-gray-600">
        ← Back to chat
      </Link>

      <div className="mb-5 flex justify-center">
        <Image src="/njdot_logo.png" alt="NJDOT" width={64} height={64} priority />
      </div>

      <h1 className="mb-1 text-center text-xl font-bold text-[#1B3A6B]">Smart Assistant</h1>
      <p className="mb-6 text-center text-sm text-gray-400">Change your password</p>

      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label htmlFor="password" className="mb-1.5 block text-xs font-medium text-gray-700">
            New password{' '}
            <span className="font-normal text-gray-400">(min. 8 characters)</span>
          </label>
          <input
            id="password"
            type="password"
            autoComplete="new-password"
            required
            minLength={8}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            disabled={isLoading}
            placeholder="••••••••"
            style={{ height: '44px' }}
            className={inputCls}
          />
        </div>

        <div>
          <label htmlFor="confirm" className="mb-1.5 block text-xs font-medium text-gray-700">
            Confirm new password
          </label>
          <input
            id="confirm"
            type="password"
            autoComplete="new-password"
            required
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            disabled={isLoading}
            placeholder="••••••••"
            style={{ height: '44px' }}
            className={inputCls}
          />
        </div>

        {error && (
          <div className="rounded-lg bg-red-50 px-3 py-2.5 text-xs text-red-700 ring-1 ring-red-200">
            {error}
          </div>
        )}

        <button
          type="submit"
          disabled={isLoading}
          style={{ height: '44px' }}
          className="w-full rounded-lg bg-[#CC2529] text-sm font-semibold text-white transition-colors hover:bg-[#a81e21] focus:outline-none focus:ring-2 focus:ring-[#CC2529]/30 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {isLoading ? 'Updating…' : 'Update Password'}
        </button>
      </form>
    </>
  )
}
