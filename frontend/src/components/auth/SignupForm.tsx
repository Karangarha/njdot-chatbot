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

export default function SignupForm() {
  const [firstName, setFirstName]             = useState('')
  const [lastName, setLastName]               = useState('')
  const [email, setEmail]                     = useState('')
  const [password, setPassword]               = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [error, setError]                     = useState<string | null>(null)
  const [isLoading, setIsLoading]             = useState(false)
  const [checkEmail, setCheckEmail]           = useState(false)
  const router = useRouter()

  const handleSubmit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    setError(null)

    if (password.length < 8) {
      setError('Password must be at least 8 characters.')
      return
    }
    if (password !== confirmPassword) {
      setError('Passwords do not match.')
      return
    }

    setIsLoading(true)

    const supabase = createClient()

    const { data, error: signUpError } = await supabase.auth.signUp({
      email,
      password,
      options: {
        data: { first_name: firstName, last_name: lastName },
        emailRedirectTo: `${window.location.origin}/auth/callback`,
      },
    })

    if (signUpError) {
      setError(signUpError.message)
      setIsLoading(false)
      return
    }

    // With "Confirm email" on, an already-registered address "succeeds" with no identities.
    if (data.user?.identities?.length === 0) {
      setError('An account with this email already exists. Try signing in.')
      setIsLoading(false)
      return
    }

    // Session is present only when confirmation is off (e.g. local dev).
    if (data.session) {
      router.push('/chat')
      router.refresh()
      return
    }

    setCheckEmail(true)
    setIsLoading(false)
  }

  if (checkEmail) {
    return (
      <div className="w-full sm:max-w-[480px]">
        <div className="rounded-2xl bg-white shadow-xl ring-1 ring-black/5 px-5 py-8 text-center sm:p-[44px]">
          <h1 className="mb-2 text-xl font-bold text-[#1B3A6B]">Check your email</h1>
          <p className="mb-6 text-sm text-gray-500">
            We sent a confirmation link to <strong>{email}</strong>. Open it to finish creating your account.
          </p>
          <Link href="/login" className="text-xs font-semibold text-[#1B3A6B] hover:underline">
            Back to sign in
          </Link>
        </div>
      </div>
    )
  }

  return (
    <div className="w-full sm:max-w-[480px]">
      {/* Card */}
      <div className="rounded-2xl bg-white shadow-xl ring-1 ring-black/5 px-5 py-8 sm:p-[44px]">

        {/* Back to home — inside card, top */}
        <Link href="/" className="mb-5 block text-[13px] text-gray-400 hover:text-gray-600">
          ← Back to home
        </Link>

        {/* Logo */}
        <div className="mb-5 flex justify-center">
          <Image src="/njdot_logo.png" alt="NJDOT" width={64} height={64} priority />
        </div>

        <h1 className="mb-1 text-center text-xl font-bold text-[#1B3A6B]">Smart Assistant</h1>
        <p className="mb-6 text-center text-sm text-gray-400">Create your account</p>

        <form onSubmit={handleSubmit} className="space-y-4">
          {/* Name row */}
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div>
              <label htmlFor="first-name" className="mb-1.5 block text-xs font-medium text-gray-700">
                First name
              </label>
              <input
                id="first-name"
                type="text"
                autoComplete="given-name"
                required
                value={firstName}
                onChange={(e) => setFirstName(e.target.value)}
                disabled={isLoading}
                placeholder="Jane"
                style={{ height: '44px' }}
                className={inputCls}
              />
            </div>
            <div>
              <label htmlFor="last-name" className="mb-1.5 block text-xs font-medium text-gray-700">
                Last name
              </label>
              <input
                id="last-name"
                type="text"
                autoComplete="family-name"
                required
                value={lastName}
                onChange={(e) => setLastName(e.target.value)}
                disabled={isLoading}
                placeholder="Smith"
                style={{ height: '44px' }}
                className={inputCls}
              />
            </div>
          </div>

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
            <label htmlFor="password" className="mb-1.5 block text-xs font-medium text-gray-700">
              Password{' '}
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

          {/* Confirm password */}
          <div>
            <label htmlFor="confirm-password" className="mb-1.5 block text-xs font-medium text-gray-700">
              Confirm password
            </label>
            <input
              id="confirm-password"
              type="password"
              autoComplete="new-password"
              required
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
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
            {isLoading ? 'Creating account…' : 'Create Account'}
          </button>
        </form>

        <p className="mt-5 text-center text-xs text-gray-500">
          Already have an account?{' '}
          <Link href="/login" className="font-semibold text-[#1B3A6B] hover:underline">
            Sign in
          </Link>
        </p>
      </div>
    </div>
  )
}
