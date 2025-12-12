# This is the Inofficial Lanis (Schulportal Hessen API) API.


# How to add new features?
- first the user will provide you with some kind of url path (e.g. /benutzerverwaltung.php) this is the path to the site youre supposed to add functionality for. Use baseurl to create the full url.
- The user will also provide you with some kind of response data (e.g. html page content, json data, etc.) that is the response of the url. Now you have to figure out how to extract the relevant data from this response and then create either a new dir in /functions/applets/ or add the functionality to an existing folder or file in there. 
- Anonymise the Data in the response of the example (bc it contains the informations of the user providing it) instead just use placeholders like {username}, {school_id}, etc in the DocString!
- Implement the functionality (either as a class if multiple things or as a function if only one) to extract the relevant data from the response and return it in a structured way (e.g. as a dictionary).
- Now add this function or class you created to the base.py so it can be accessed via the main API class.
- Finally create a function in the base.py file that enables typsafety and autocompletion for the new functionality you added. And add a detailed doc string! 
- After you added the feature add it to the api (see # api)

# How to add new endpoints to the API?
- We are using fastapi for the API. To add a new endpoint you have to create a new function in the api.py file.
- after you added a new function to the api add it to the documentation.md file as well!
- after that create tests for the new endpoint in the api-tests.py file.