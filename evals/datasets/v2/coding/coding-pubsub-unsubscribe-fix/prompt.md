`Broker` in `pubsub.py` is a tiny publish/subscribe hub. Reported problems: a subscriber that unsubscribes itself while handling a message makes the next subscriber miss that message; calling an unsubscribe function twice raises `ValueError`; and when the same callback is subscribed twice, unsubscribing the second subscription removes the first one.

Fix it so that:

- `subscribe(topic, callback)` returns an unsubscribe function that removes exactly that one subscription. Calling it again does nothing. The same callback subscribed twice is two independent subscriptions, each called once per message.
- `publish(topic, message)` calls the subscribers in subscription order and returns the number of callbacks it called (0 for a topic nobody listens to).
- A subscriber unsubscribed during a publish (by itself or another) is not called later in that publish; a subscriber added during a publish is first called by the next publish.
- An exception from a callback propagates at once and the remaining subscribers are not called.
