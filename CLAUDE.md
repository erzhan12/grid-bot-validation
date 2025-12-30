# Workflow
Always follow the workflow listed below in order and try to understand which step you're on so that you can stay on task and follow our development process.

1. Every problem that the user comes with you to work on needs to be clearly defined and documented.

Make sure that before you start work on something you've identified the specific task or problem the user wants to work on. Work with the user to clearly articulate what needs to be accomplished.

Step one is to identify and document the specific task or problem we're solving.

2. After identifying the task, I want you to research within our code base. Search for similar implementations or related functionality, and then also search the RUES.md file at this path: ./RULES.md in order to gather useful information so that we create reusable, modular, and DRY (Don't Repeat Yourself) code.

3. After researching the code base and the RULES.md file, I want you to let me know what your plan is for implementing the change or the bug fix that I mentioned. Confirm with me before making changes to the codebase.

4. Once you have a plan and confirmation to begin working, you can start implementing the changes based on the task we defined in step one.

5. Begin implementing your changes based on the RULES.md file from what you read, and also the patterns that you already found in the code base from step two. As you make changes, verify with the user that the changes look good and have the user test them manually on the development version of the site. Once the user confirms that the changes look good and the feature is working or the bug is fixed, then go ahead and make a commit with a clear, descriptive commit message.

6. As you're making changes, update the RULES.md file with short bits of information on how the code base works and pitfalls to avoid that you ran into during this process. There will likely be a back-and-forth with the user and you may attempt something many times—if it seems like something that you might forget again in the future if you didn't have the current context, make sure to add that to RULES.md. Include file paths that are relevant and task descriptions. If we develop a way of doing something with a component or style, make sure to document that in RULES.md. We don't want to make RULES.md overly complex. We want to make it a clear instruction manual for future developers, and for yourself once you lose the context of this session. It's also OK to remove things from the RULES.md file if they no longer apply or if we change the way of doing something. It's crucial that we keep this file up-to-date with our latest developments, so that's why this is step six—so that we can look at everything we did and look at how we got there and then add notes to the RULES.md as a scratch pad for future coding sessions.

7. After making all necessary commits for this particular task, verify with the user that they're satisfied with the implementation and that the task is complete.

8. At this point if the user is satisfied and the changes are committed, ask if they're ready to move on to the next task or if there's anything else they'd like to adjust with the current implementation.