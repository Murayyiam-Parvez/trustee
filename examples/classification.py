# importing required libraries
# importing Scikit-learn library and datasets package
import graphviz
import pandas as pd

from sklearn import tree
from sklearn import datasets
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report


from trustee import ClassificationTrustee

# Loading the iris plants dataset (classification)
iris = datasets.load_iris()
# dividing the datasets into two parts i.e. training datasets and test datasets
X, y = datasets.load_iris(return_X_y=True)

# Spliting arrays or matrices into random train and test subsets
# i.e. 70 % training dataset and 30 % test datasets
X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.30)

# creating dataframe of IRIS dataset
data = pd.DataFrame(
    {
        "sepallength": iris.data[:, 0],
        "sepalwidth": iris.data[:, 1],
        "petallength": iris.data[:, 2],
        "petalwidth": iris.data[:, 3],
        "species": iris.target,
    }
)

# creating a RF classifier
clf = RandomForestClassifier(n_estimators=100)
# Training the model on the training dataset
# fit function is used to train the model using the training sets as parameters
clf.fit(X_train, y_train)
# performing predictions on the test dataset
y_pred = clf.predict(X_test)

# Initialize Trustee and fit for classification models
trustee = ClassificationTrustee(expert=clf)
trustee.fit(X_train, y_train, num_iter=50, samples_size=0.3, verbose=True)

# Get the best explanation from Trustee
dt, reward, idx = trustee.explain()
print(f"Model explanation {idx} training fidelity: {reward}")

# Get the Top-10 pruned explanation DT
pruned_dt = trustee.prune(top_k=10)
print(f"Top-k Prunned explanation size: {dt.tree_.node_count}")

# Use explanations to make predictions
dt_y_pred = dt.predict(X_test)
pruned_dt_y_pred = pruned_dt.predict(X_test)

# Evaluate accuracy and fidelity of explanations
print("Model explanation global fidelity report:")
print(classification_report(y_pred, dt_y_pred))
print("Top-k Model explanation global fidelity report:")
print(classification_report(y_pred, pruned_dt_y_pred))

print("Model explanation score report:")
print(classification_report(y_test, dt_y_pred))
print("Top-k Model explanation score report:")
print(classification_report(y_test, pruned_dt_y_pred))


# Output decision tree to pdf
dot_data = tree.export_graphviz(
    dt,
    class_names=iris.target_names,
    feature_names=iris.feature_names,
    filled=True,
    rounded=True,
    special_characters=True,
)
graph = graphviz.Source(dot_data)
graph.render("dt_explanation")

# Output pruned decision tree to pdf
dot_data = tree.export_graphviz(
    pruned_dt,
    class_names=iris.target_names,
    feature_names=iris.feature_names,
    filled=True,
    rounded=True,
    special_characters=True,
)
graph = graphviz.Source(dot_data)
graph.render("pruned_dt_explation")