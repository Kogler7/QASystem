from init_database.init_database import DatabaseInit
if __name__ == "__main__":
    init = DatabaseInit()
    init.create_collection()
    init.insert_data()
