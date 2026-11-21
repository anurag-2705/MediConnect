import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { Bell, BellOff, CalendarClock, CalendarPlus, Heart, Star } from 'lucide-react'
import { ApiError, api, dateTime, doctorDisplayName, indiaDate, post } from './api'
import type { Appointment, Doctor, Review, Slot } from './api'
import { ErrorBox, Field, useAuth } from './Account'
import { ActionButton, Avatar, Loading } from './ui'
import './extras.css'

/* ------------------------------------------------------------------ saved doctors */

type FavoritesValue = { ids: Set<number>; ready: boolean; canSave: boolean; toggle: (doctorId: number) => Promise<string | null> }
const FavoritesContext = createContext<FavoritesValue>({ ids: new Set(), ready: false, canSave: false, toggle: async () => null })

export function FavoritesProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth()
  const canSave = user?.role === 'patient' && !user.verification_required
  const [ids, setIds] = useState<Set<number>>(new Set())
  const [ready, setReady] = useState(false)
  useEffect(() => {
    setIds(new Set())
    setReady(false)
    if (!canSave) return
    const controller = new AbortController()
    api<number[]>('/favorites/ids', { signal: controller.signal })
      .then(rows => { setIds(new Set(rows)); setReady(true) })
      .catch(() => { /* the heart simply stays hidden if this fails */ })
    return () => controller.abort()
  }, [canSave, user?.id])
  const toggle = useCallback(async (doctorId: number) => {
    const saved = ids.has(doctorId)
    try {
      await api(`/favorites/${doctorId}`, { method: saved ? 'DELETE' : 'PUT' })
      setIds(current => {
        const next = new Set(current)
        if (saved) next.delete(doctorId)
        else next.add(doctorId)
        return next
      })
      return null
    } catch (err) { return (err as Error).message }
  }, [ids])
  return <FavoritesContext.Provider value={{ ids, ready, canSave, toggle }}>{children}</FavoritesContext.Provider>
}

export const useFavorites = () => useContext(FavoritesContext)

export function FavoriteButton({ doctorId, name }: { doctorId: number; name: string }) {
  const { ids, ready, canSave, toggle } = useFavorites()
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState('')
  if (!canSave || !ready) return null
  const saved = ids.has(doctorId)
  return <>
    <button type="button" className={`favorite-button${saved ? ' is-saved' : ''}`} aria-pressed={saved} disabled={busy}
      aria-label={saved ? `Remove ${name} from saved doctors` : `Save ${name} to saved doctors`}
      onClick={async () => {
        setBusy(true)
        const failure = await toggle(doctorId)
        setNote(failure ?? (saved ? 'Removed from saved doctors.' : 'Saved to your doctors.'))
        setBusy(false)
      }}>
      <Heart size={18} fill={saved ? 'currentColor' : 'none'} aria-hidden="true" />
    </button>
    <span className="visually-hidden" role="status">{note}</span>
  </>
}

/* ------------------------------------------------------------------ waitlist */

type WaitlistRow = { id: number; doctor_id: number; doctor_name: string; mode: 'clinic' | 'online'; created_at: string }

export function WaitlistPanel({ doctorId, mode }: { doctorId: number; mode: 'clinic' | 'online' }) {
  const { user } = useAuth()
  const eligible = user?.role === 'patient' && !user.verification_required
  const [joined, setJoined] = useState<boolean | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    setJoined(null)
    setError('')
    if (!eligible) return
    const controller = new AbortController()
    api<WaitlistRow[]>('/waitlist', { signal: controller.signal })
      .then(rows => setJoined(rows.some(row => row.doctor_id === doctorId && row.mode === mode)))
      .catch(() => { if (!controller.signal.aborted) setJoined(false) })
    return () => controller.abort()
  }, [doctorId, mode, eligible])
  if (!eligible) return null
  const change = async () => {
    setBusy(true)
    setError('')
    try {
      if (joined) await api(`/doctors/${doctorId}/waitlist?mode=${mode}`, { method: 'DELETE' })
      else await post(`/doctors/${doctorId}/waitlist`, { mode })
      setJoined(!joined)
    } catch (err) { setError((err as Error).message) }
    finally { setBusy(false) }
  }
  return <div className="waitlist-panel">
    <p>{joined ? 'We will notify you in MediConnect as soon as a time opens up.' : 'Nothing is open right now. Get notified if a time becomes available.'}</p>
    {error && <ErrorBox message={error} />}
    <ActionButton type="button" className="outline-button small" pending={busy} pendingLabel="Updating…" disabled={joined === null}
      onClick={() => void change()}>
      {joined ? <><BellOff size={16} aria-hidden="true" /> Stop notifying me</> : <><Bell size={16} aria-hidden="true" /> Notify me</>}
    </ActionButton>
  </div>
}

/* ------------------------------------------------------------------ appointment extras */

export function CalendarLink({ item }: { item: Appointment }) {
  return <a className="outline-button small" href={`/api/v1/appointments/${item.id}/calendar.ics`} download={`appointment-${item.id}.ics`}>
    <CalendarPlus size={16} aria-hidden="true" /> Add to calendar
  </a>
}

export function RescheduleDialog({ item, close, onMoved }: { item: Appointment; close: () => void; onMoved: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [slots, setSlots] = useState<Slot[] | null>(null)
  const [choice, setChoice] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [revision, setRevision] = useState(0)
  useEffect(() => { dialog.current?.showModal() }, [])
  useEffect(() => {
    const controller = new AbortController()
    api<Slot[]>(`/doctors/${item.doctor_id}/slots`, { signal: controller.signal })
      .then(rows => setSlots(rows.filter(slot => slot.mode === item.mode && slot.id !== item.slot_id)))
      .catch(err => { if (!controller.signal.aborted) setError(err.message) })
    return () => controller.abort()
  }, [item.doctor_id, item.mode, item.slot_id, revision])
  const move = async (event: FormEvent) => {
    event.preventDefault()
    if (!choice) return
    setBusy(true)
    setError('')
    try {
      await post(`/appointments/${item.id}/reschedule`, { new_slot_id: Number(choice) })
      onMoved()
      close()
    } catch (err) {
      setError((err as Error).message)
      if (err instanceof ApiError && err.status === 409) {
        setChoice('')
        setSlots(null)
        setRevision(value => value + 1)
      }
    } finally { setBusy(false) }
  }
  return <dialog ref={dialog} className="cancel-dialog" aria-label="Reschedule appointment" onCancel={event => {
    event.preventDefault()
    if (!busy) close()
  }}>
    <h2>Choose a new time</h2>
    <p>{doctorDisplayName(item.doctor_name)} · currently {dateTime(item.start_at)} IST</p>
    <p className="muted">Moving is free at least 24 hours ahead. You keep the same doctor, visit type and fee.</p>
    {slots === null && !error ? <Loading inline>Loading available times…</Loading>
      : slots && !slots.length ? <p className="slot-empty">No other {item.mode === 'online' ? 'video' : 'clinic'} times are open with this doctor right now.</p>
      : slots && <form onSubmit={move} className="reschedule-form">
        <label>New time (IST)
          <select value={choice} onChange={event => setChoice(event.target.value)} required disabled={busy}>
            <option value="" disabled>Select a time</option>
            {slots.map(slot => <option key={slot.id} value={slot.id}>{dateTime(slot.start_at)}</option>)}
          </select>
        </label>
        {error && <ErrorBox message={error} />}
        <div className="cancel-actions">
          <button type="button" className="outline-button small" disabled={busy} onClick={close}>Keep current time</button>
          <ActionButton className="button button-small" pending={busy} pendingLabel="Moving…" disabled={!choice}>Confirm new time</ActionButton>
        </div>
      </form>}
    {(slots === null || !slots.length) && <>
      {error && <ErrorBox message={error} />}
      <div className="cancel-actions"><button type="button" className="outline-button small" autoFocus onClick={close}>Close</button></div>
    </>}
  </dialog>
}

/* ------------------------------------------------------------------ doctor: repeating availability */

const WEEKDAYS = [['Mon', 0], ['Tue', 1], ['Wed', 2], ['Thu', 3], ['Fri', 4], ['Sat', 5], ['Sun', 6]] as const

export function BulkSlotForm({ disabled, onCreated }: { disabled: boolean; onCreated: () => void }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const today = indiaDate(new Date().toISOString())
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const form = event.currentTarget
    const data = new FormData(form)
    const weekdays = data.getAll('weekday').map(Number)
    setError('')
    setMessage('')
    if (!weekdays.length) { setError('Pick at least one weekday.'); return }
    setBusy(true)
    try {
      const result = await post<{ created: number; skipped: number }>('/doctor/slots/bulk', {
        mode: data.get('mode'), start_date: data.get('start_date'), end_date: data.get('end_date'), weekdays,
        start_time: data.get('start_time'), end_time: data.get('end_time'), slot_minutes: Number(data.get('slot_minutes')),
      })
      setMessage(`Created ${result.created} ${result.created === 1 ? 'slot' : 'slots'}${result.skipped ? `; skipped ${result.skipped} that already existed or have passed` : ''}.`)
      onCreated()
    } catch (err) { setError((err as Error).message) }
    finally { setBusy(false) }
  }
  return <form className="stack-form bulk-form" onSubmit={submit} aria-label="Repeat weekly availability">
    <label>Visit type<select name="mode"><option value="clinic">Clinic visit</option><option value="online">Video consultation</option></select></label>
    <div className="bulk-row">
      <Field label="From date" type="date" name="start_date" min={today} defaultValue={today} required />
      <Field label="To date" type="date" name="end_date" min={today} required />
    </div>
    <fieldset className="weekday-picker"><legend>Repeat on</legend>
      {WEEKDAYS.map(([label, value]) => <label key={value}><input type="checkbox" name="weekday" value={value} defaultChecked={value < 5} /> {label}</label>)}
    </fieldset>
    <div className="bulk-row">
      <Field label="Start time" type="time" name="start_time" defaultValue="10:00" required />
      <Field label="End time" type="time" name="end_time" defaultValue="13:00" required />
    </div>
    <label>Length of each slot<select name="slot_minutes" defaultValue="30">{[15, 20, 30, 45, 60].map(minutes => <option key={minutes} value={minutes}>{minutes} minutes</option>)}</select></label>
    {error && <ErrorBox message={error} />}
    {message && <p className="success-box" role="status">{message}</p>}
    <ActionButton className="button" pending={busy} pendingLabel="Creating slots…" disabled={disabled}>Create repeating slots</ActionButton>
  </form>
}

/* ------------------------------------------------------------------ doctor: reply to reviews */

export function DoctorReviews({ doctorId }: { doctorId: number }) {
  const [reviews, setReviews] = useState<Review[] | null>(null)
  const [error, setError] = useState('')
  const [revision, setRevision] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    api<Doctor>(`/doctors/${doctorId}`, { signal: controller.signal })
      .then(doctor => { if (!controller.signal.aborted) setReviews(doctor.reviews ?? []) })
      .catch(err => { if (!controller.signal.aborted) setError(err instanceof ApiError && err.status === 404 ? 'Reviews appear here once your profile is published.' : err.message) })
    return () => controller.abort()
  }, [doctorId, revision])
  if (error) return <section className="content-card"><h2>Patient reviews</h2><p className="muted">{error}</p></section>
  if (!reviews) return <Loading>Loading reviews…</Loading>
  return <section className="content-card doctor-reviews"><h2>Patient reviews</h2>
    {reviews.length ? reviews.map(review => <ReviewReply key={review.id} review={review} onSaved={() => setRevision(value => value + 1)} />)
      : <p className="muted">No reviews yet. Patients can review after a completed visit.</p>}
  </section>
}

function ReviewReply({ review, onSaved }: { review: Review; onSaved: () => void }) {
  const [editing, setEditing] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const save = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      await post(`/reviews/${review.id}/reply`, { reply: String(new FormData(event.currentTarget).get('reply') ?? '') })
      setEditing(false)
      onSaved()
    } catch (err) { setError((err as Error).message) }
    finally { setBusy(false) }
  }
  return <div className="review">
    <span className="rating"><Star size={15} fill="currentColor" aria-hidden="true" /> {review.rating}/5</span>
    {review.comment && <p>{review.comment}</p>}
    {review.doctor_reply && !editing && <p className="doctor-reply"><b>Your reply:</b> {review.doctor_reply}</p>}
    {editing ? <form className="review-form" onSubmit={save}>
      <label>Your reply<textarea name="reply" rows={3} minLength={2} maxLength={500} defaultValue={review.doctor_reply ?? ''} required /></label>
      {error && <ErrorBox message={error} />}
      <div className="inline-actions">
        <ActionButton className="button button-small" pending={busy} pendingLabel="Saving…">Post reply</ActionButton>
        <button type="button" className="text-button" disabled={busy} onClick={() => setEditing(false)}>Cancel</button>
      </div>
    </form> : <button type="button" className="text-button" onClick={() => setEditing(true)}>{review.doctor_reply ? 'Edit reply' : 'Reply'}</button>}
  </div>
}

/* ------------------------------------------------------------------ patient: saved doctors and waitlist */

export function SavedAndWaiting() {
  const { ids, canSave } = useFavorites()
  const [saved, setSaved] = useState<Doctor[] | null>(null)
  const [waiting, setWaiting] = useState<WaitlistRow[] | null>(null)
  const [error, setError] = useState('')
  const [revision, setRevision] = useState(0)
  const savedKey = [...ids].sort().join(',')
  useEffect(() => {
    if (!canSave) return
    const controller = new AbortController()
    Promise.all([api<Doctor[]>('/favorites', { signal: controller.signal }), api<WaitlistRow[]>('/waitlist', { signal: controller.signal })])
      .then(([doctors, rows]) => { if (!controller.signal.aborted) { setSaved(doctors); setWaiting(rows) } })
      .catch(err => { if (!controller.signal.aborted) setError(err.message) })
    return () => controller.abort()
  }, [canSave, savedKey, revision])
  if (!canSave) return null
  const leave = async (row: WaitlistRow) => {
    setError('')
    try {
      await api(`/doctors/${row.doctor_id}/waitlist?mode=${row.mode}`, { method: 'DELETE' })
      setRevision(value => value + 1)
    } catch (err) { setError((err as Error).message) }
  }
  if (saved === null && waiting === null && !error) return null
  if (!saved?.length && !waiting?.length && !error) return null
  return <section className="dashboard-section saved-section" aria-labelledby="saved-doctors-title">
    <h2 id="saved-doctors-title">Saved doctors and waitlist</h2>
    {error && <ErrorBox message={error} />}
    <div className="saved-grid">
      {saved?.map(doctor => <Link key={doctor.id} className="saved-card" to={`/doctors/${doctor.id}`}>
        <Avatar name={doctor.full_name} />
        <span><b>{doctorDisplayName(doctor.full_name)}</b><small>{doctor.specialty} · {doctor.city}</small></span>
      </Link>)}
      {waiting?.map(row => <div key={row.id} className="saved-card waiting-card">
        <span className="mini-icon"><CalendarClock size={18} aria-hidden="true" /></span>
        <span><b>Waiting: {doctorDisplayName(row.doctor_name)}</b><small>{row.mode === 'online' ? 'Video visit' : 'Clinic visit'} · we will notify you</small></span>
        <button type="button" className="text-button" onClick={() => void leave(row)}>Stop</button>
      </div>)}
    </div>
  </section>
}
