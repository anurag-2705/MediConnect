import { render, screen, waitFor, within, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthContext } from './Account'
import { BulkSlotForm, FavoriteButton, FavoritesProvider, RescheduleDialog } from './Extras'
import type { Appointment, Slot, User } from './api'

type Handler = (url: string, init: RequestInit) => { status?: number; body: unknown } | undefined
let calls: { url: string; method: string; body: unknown }[] = []

function mockApi(handler: Handler) {
  calls = []
  vi.stubGlobal('fetch', vi.fn(async (input: string, init: RequestInit = {}) => {
    const url = String(input).replace('/api/v1', '')
    calls.push({ url, method: init.method ?? 'GET', body: typeof init.body === 'string' ? JSON.parse(init.body) : undefined })
    const reply = handler(url, init) ?? { status: 404, body: { detail: 'Not found' } }
    return new Response(JSON.stringify(reply.body), { status: reply.status ?? 200, headers: { 'Content-Type': 'application/json' } })
  }))
}

const account = (role: User['role']): User => ({ id: 1, email: 'a@example.com', full_name: 'Asha', phone: '', role,
  verification_status: null, verification_required: false, contacts: {} as User['contacts'] })
const asUser = (user: User | null, children: React.ReactNode) =>
  <AuthContext.Provider value={{ user, setUser: () => {} }}><FavoritesProvider>{children}</FavoritesProvider></AuthContext.Provider>

beforeEach(() => { calls = [] })
afterEach(() => vi.unstubAllGlobals())

describe('FavoriteButton', () => {
  it('saves and removes a doctor and reflects it in the pressed state', async () => {
    const saved = new Set<number>()
    mockApi((url, init) => {
      if (url === '/favorites/ids') return { body: [...saved] }
      const match = url.match(/^\/favorites\/(\d+)$/)
      if (match && init.method === 'PUT') { saved.add(Number(match[1])); return { body: { favorite: true } } }
      if (match && init.method === 'DELETE') { saved.delete(Number(match[1])); return { body: { favorite: false } } }
    })
    const user = userEvent.setup()
    render(asUser(account('patient'), <FavoriteButton doctorId={5} name="Dr. Rao" />))
    const save = await screen.findByRole('button', { name: 'Save Dr. Rao to saved doctors' })
    expect(save).toHaveAttribute('aria-pressed', 'false')
    await user.click(save)
    const remove = await screen.findByRole('button', { name: 'Remove Dr. Rao from saved doctors' })
    expect(remove).toHaveAttribute('aria-pressed', 'true')
    expect(calls.some(call => call.url === '/favorites/5' && call.method === 'PUT')).toBe(true)
    await user.click(remove)
    expect(await screen.findByRole('button', { name: 'Save Dr. Rao to saved doctors' })).toBeInTheDocument()
    expect(calls.some(call => call.url === '/favorites/5' && call.method === 'DELETE')).toBe(true)
  })

  it('shows nothing to signed-out visitors and doctors, and makes no request for them', async () => {
    mockApi(() => ({ body: [] }))
    const { rerender } = render(asUser(null, <FavoriteButton doctorId={5} name="Dr. Rao" />))
    rerender(asUser(account('doctor'), <FavoriteButton doctorId={5} name="Dr. Rao" />))
    await new Promise(resolve => setTimeout(resolve, 20))
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
    expect(calls).toHaveLength(0)
  })

  it('keeps the old state and reports nothing misleading when the server refuses', async () => {
    mockApi((url, init) => {
      if (url === '/favorites/ids') return { body: [] }
      if (init.method === 'PUT') return { status: 409, body: { detail: 'You can save up to 100 doctors' } }
    })
    const user = userEvent.setup()
    render(asUser(account('patient'), <FavoriteButton doctorId={5} name="Dr. Rao" />))
    await user.click(await screen.findByRole('button', { name: /save dr\. rao/i }))
    expect(await screen.findByRole('status')).toHaveTextContent('You can save up to 100 doctors')
    expect(screen.getByRole('button', { name: /save dr\. rao/i })).toHaveAttribute('aria-pressed', 'false')
  })
})

describe('BulkSlotForm', () => {
  it('sends the repeating schedule exactly as the API expects and reports the result', async () => {
    mockApi(url => url === '/doctor/slots/bulk' ? { status: 201, body: { created: 12, skipped: 2, slots: [] } } : undefined)
    const onCreated = vi.fn()
    const user = userEvent.setup()
    render(<BulkSlotForm disabled={false} onCreated={onCreated} />)
    fireEvent.change(screen.getByLabelText('To date'), { target: { value: '2099-01-10' } })
    await user.click(screen.getByRole('checkbox', { name: 'Sat' }))
    await user.selectOptions(screen.getByLabelText('Length of each slot'), '45')
    await user.click(screen.getByRole('button', { name: 'Create repeating slots' }))
    expect(await screen.findByText(/Created 12 slots; skipped 2/)).toBeInTheDocument()
    const body = calls.find(call => call.url === '/doctor/slots/bulk')!.body as Record<string, unknown>
    expect(body).toMatchObject({ mode: 'clinic', end_date: '2099-01-10', weekdays: [0, 1, 2, 3, 4, 5],
      start_time: '10:00', end_time: '13:00', slot_minutes: 45 })
    expect(onCreated).toHaveBeenCalledOnce()
  })

  it('refuses to submit with no weekday selected and does not call the server', async () => {
    mockApi(() => ({ body: {} }))
    const user = userEvent.setup()
    render(<BulkSlotForm disabled={false} onCreated={() => {}} />)
    fireEvent.change(screen.getByLabelText('To date'), { target: { value: '2099-01-10' } })
    for (const day of ['Mon', 'Tue', 'Wed', 'Thu', 'Fri']) await user.click(screen.getByRole('checkbox', { name: day }))
    await user.click(screen.getByRole('button', { name: 'Create repeating slots' }))
    expect(await screen.findByText('Pick at least one weekday.')).toBeInTheDocument()
    expect(calls).toHaveLength(0)
  })

  it('shows the server message when the schedule is rejected', async () => {
    mockApi(() => ({ status: 422, body: { detail: 'That would create 300 slots. Create at most 200 at a time.' } }))
    const user = userEvent.setup()
    render(<BulkSlotForm disabled={false} onCreated={() => {}} />)
    fireEvent.change(screen.getByLabelText('To date'), { target: { value: '2099-01-10' } })
    await user.click(screen.getByRole('button', { name: 'Create repeating slots' }))
    expect(await screen.findByText(/Create at most 200/)).toBeInTheDocument()
  })

  it('is disabled until the doctor profile is published', () => {
    mockApi(() => ({ body: {} }))
    render(<BulkSlotForm disabled onCreated={() => {}} />)
    expect(screen.getByRole('button', { name: 'Create repeating slots' })).toBeDisabled()
  })
})

describe('RescheduleDialog', () => {
  const item = { id: 9, slot_id: 1, doctor_id: 4, doctor_name: 'Dr. Rao', mode: 'online', start_at: '2099-01-05T05:00:00Z' } as Appointment
  const slot = (id: number, mode: Slot['mode'], start_at: string): Slot => ({ id, doctor_id: 4, mode, start_at, end_at: start_at, status: 'available' })
  const slots = [slot(1, 'online', '2099-01-05T05:00:00Z'), slot(2, 'online', '2099-01-07T05:00:00Z'), slot(3, 'clinic', '2099-01-08T05:00:00Z')]

  it('offers only other times of the same visit type and moves the booking', async () => {
    mockApi((url, init) => {
      if (url === '/doctors/4/slots') return { body: slots }
      if (url === '/appointments/9/reschedule' && init.method === 'POST') return { body: {} }
    })
    const onMoved = vi.fn()
    const close = vi.fn()
    const user = userEvent.setup()
    render(<RescheduleDialog item={item} close={close} onMoved={onMoved} />)
    const select = await screen.findByLabelText(/New time/)
    expect(within(select).getAllByRole('option').map(option => option.getAttribute('value'))).toEqual(['', '2'])
    expect(screen.getByRole('button', { name: 'Confirm new time' })).toBeDisabled()
    await user.selectOptions(select, '2')
    await user.click(screen.getByRole('button', { name: 'Confirm new time' }))
    await waitFor(() => expect(onMoved).toHaveBeenCalledOnce())
    expect(calls.find(call => call.url === '/appointments/9/reschedule')!.body).toEqual({ new_slot_id: 2 })
    expect(close).toHaveBeenCalled()
  })

  it('explains when nothing else is open', async () => {
    mockApi(url => url === '/doctors/4/slots' ? { body: [slots[0], slots[2]] } : undefined)
    render(<RescheduleDialog item={item} close={() => {}} onMoved={() => {}} />)
    expect(await screen.findByText(/No other video times are open/)).toBeInTheDocument()
  })

  it('shows the conflict, refreshes the list and keeps the dialog open when the time was just taken', async () => {
    let listings = 0
    mockApi((url, init) => {
      if (url === '/doctors/4/slots') { listings += 1; return { body: listings === 1 ? slots : [slots[0]] } }
      if (init.method === 'POST') return { status: 409, body: { detail: 'That time is no longer available' } }
    })
    const onMoved = vi.fn()
    const user = userEvent.setup()
    render(<RescheduleDialog item={item} close={() => {}} onMoved={onMoved} />)
    await user.selectOptions(await screen.findByLabelText(/New time/), '2')
    await user.click(screen.getByRole('button', { name: 'Confirm new time' }))
    expect(await screen.findByText('That time is no longer available')).toBeInTheDocument()
    await waitFor(() => expect(listings).toBe(2))
    expect(onMoved).not.toHaveBeenCalled()
  })
})
