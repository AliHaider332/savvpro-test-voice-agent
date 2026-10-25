# Build a Voice Assistant

Pick a job that people usually do on the phone. Build a voice assistant that can do that job for them.

## 1. How it works

The caller talks. The assistant listens, decides what to do, and talks back.

```
Caller speaks → Speech to text → The brain → Text to speech → Caller hears the reply
```

This repeats every turn until the call ends.

> **The one big rule:** the brain must be your own code. Do not use a big AI model (like GPT, Claude or Gemini) to decide what happens. Your code decides what the caller wants, what to ask next, and what to do.

You can use AI tools to help you write the code. We want to see how you use them.

## 2. Pick a use case

You can pick anything. The assistant should do a real job from start to end, not just chat.

> **Example:** booking a table at a restaurant. The assistant asks for the day, the time and how many people, checks if a table is free, books it, and says goodbye.

Real callers are messy. Think about what can go wrong in your use case and make your assistant handle it. For example, in a restaurant booking the caller might change the time halfway through. Finding these cases is part of the task.

## 3. Tech

- Backend: Python.
- Frontend: React or Next.js. A simple page where someone can talk to the assistant and see what it decided.
- Everything else is up to you. Use any framework, library, API or service you want, for speech to text, text to speech, audio, data, testing, or anything else. This does not include the brain: no big AI model for decisions (see the big rule above).

## 4. Your repo should include

- `README.md`: how to set it up and run it.
- `DESIGN.md`: your use case and how your brain makes decisions.
- `AI_USAGE.md`: which AI tools you used, how they helped, where they got things wrong, and how many tokens you used. Add the token logs or usage screenshots from your tools.

When you are done, open a pull request with your work.

## 5. Codebase rules

- Do not commit keys, passwords or tokens.
- Use fake data only. No real people's details.
- Add tests.

**Nice to have:** build it like a real production product. A clean project structure and the good practices you would use in a live product will earn extra points.

Good luck, and have fun with it.
